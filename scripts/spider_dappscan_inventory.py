"""Fetch, verify, and inventory the complete DAppSCAN Solidity snapshot.

The snapshot command downloads the pinned GitHub archive into a run-scoped
staging directory, obtains the complete remote tree with a blobless Git fetch,
extracts only ``DAppSCAN-source/contracts``, and promotes the verified source
to the requested external directory.  The inventory command is also useful on
small local fixtures and has no Spider or third-party dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

SCHEMA = "spider-dappscan-inventory/1"
SNAPSHOT_SCHEMA = "spider-dappscan-snapshot/1"
REPOSITORY = "https://github.com/InPlusLab/DAppSCAN.git"
ARCHIVE_BASE = "https://github.com/InPlusLab/DAppSCAN/archive"
DEFAULT_COMMIT = "66a56619c44770e05c2db600fa6468115ff0dcd5"
CONTRACTS_PREFIX = "DAppSCAN-source/contracts/"
EXPECTED_SOLIDITY_FILES = 21458
EXPECTED_PROJECTS = 682

CONFIG_NAMES = {
    "foundry.toml",
    "remappings.txt",
    "hardhat.config.js",
    "hardhat.config.ts",
    "truffle-config.js",
    "truffle.js",
    "brownie-config.yaml",
    "brownie-config.yml",
    "brownie-config.py",
    "ape-config.yaml",
    "ape-config.yml",
    "package.json",
    "yarn.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "npm-shrinkwrap.json",
    "dockerfile",
}
LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1"
SOLIDITY_SUFFIX = ".sol"
_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be verified without weakening a gate."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="",
    )
    os.replace(temporary, path)


def _normalise_relative(path: Path) -> str:
    return path.as_posix()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _decode_source(data: bytes) -> tuple[str | None, str | None]:
    """Return an encoding label and decoded text, preserving invalid errors."""

    for encoding in ("utf-8-sig", "utf-16", "utf-32", "latin-1"):
        try:
            return encoding, data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None, None


def _lex_solidity(text: str) -> tuple[list[str], list[str]]:
    """Collect Solidity pragmas and imports without matching comments.

    This is intentionally a small lexical scanner rather than a Solidity
    parser. It skips comments, tokenises quoted strings, and then reads only
    ``pragma`` and ``import`` statements terminated by semicolons.
    """

    tokens: list[tuple[str, str]] = []
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i + 2)
            i = length if end < 0 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = length if end < 0 else end + 2
            continue
        if char in "'\"":
            quote = char
            start = i
            i += 1
            value: list[str] = []
            while i < length:
                if text[i] == "\\" and i + 1 < length:
                    value.append(text[i : i + 2])
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                value.append(text[i])
                i += 1
            tokens.append(("string", "".join(value)))
            if i == start:
                i += 1
            continue
        match = _IDENTIFIER_RE.match(text, i)
        if match:
            tokens.append(("word", match.group(0)))
            i = match.end()
            continue
        tokens.append(("symbol", char))
        i += 1

    pragmas: list[str] = []
    imports: list[str] = []
    index = 0
    while index < len(tokens):
        kind, value = tokens[index]
        if kind != "word" or value not in {"pragma", "import"}:
            index += 1
            continue
        statement = []
        cursor = index + 1
        while cursor < len(tokens):
            token_kind, token_value = tokens[cursor]
            if token_value == ";":
                break
            statement.append((token_kind, token_value))
            cursor += 1
        if value == "pragma" and statement and statement[0] == ("word", "solidity"):
            pragmas.append(" ".join(item[1] for item in statement[1:]))
        elif value == "import":
            strings = [item[1] for item in statement if item[0] == "string"]
            if strings:
                imports.append(strings[-1])
        index = cursor + 1 if cursor < len(tokens) else len(tokens)
    return pragmas, imports


def _is_config(path: Path) -> bool:
    name = path.name.lower()
    return name in CONFIG_NAMES or name.endswith((".config.js", ".config.ts", ".toml"))


def _project_id(relative_path: PurePosixPath) -> str:
    parts = relative_path.parts
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0] if parts else "<root>"


def _file_record(
    path: Path,
    source_root: Path,
    *,
    remote: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    relative = PurePosixPath(_normalise_relative(path.relative_to(source_root)))
    data = path.read_bytes()
    encoding, decoded = _decode_source(data)
    is_solidity = path.suffix.lower() == SOLIDITY_SUFFIX
    pragmas: list[str] = []
    imports: list[str] = []
    errors: list[str] = []
    if is_solidity:
        if decoded is None:
            errors.append("undecodable_source")
        else:
            pragmas, imports = _lex_solidity(decoded)
    if data.startswith(LFS_PREFIX):
        errors.append("git_lfs_pointer")
    record: dict[str, Any] = {
        "project_id": _project_id(relative),
        "path": relative.as_posix(),
        "kind": "solidity" if is_solidity else ("config" if _is_config(path) else "other"),
        "sha256": _sha256_bytes(data),
        "git_blob_sha1": _git_blob_sha1(data),
        "bytes": len(data),
        "encoding": encoding,
        "lfs_pointer": data.startswith(LFS_PREFIX),
        "pragmas": pragmas,
        "imports": imports,
        "errors": errors,
    }
    if remote is not None:
        record["remote_mode"] = remote.get("mode")
        record["remote_type"] = remote.get("type")
        record["remote_git_blob_sha1"] = remote.get("sha")
        if remote.get("type") != "blob":
            record["errors"].append("remote_entry_not_blob")
        elif remote.get("sha") != record["git_blob_sha1"]:
            record["errors"].append("git_blob_sha1_mismatch")
    return record


def _missing_record(relative: str, remote: Mapping[str, Any]) -> dict[str, Any]:
    path = PurePosixPath(relative)
    is_solidity = path.suffix.lower() == SOLIDITY_SUFFIX
    return {
        "project_id": _project_id(path),
        "path": relative,
        "kind": "solidity" if is_solidity else ("config" if _is_config(Path(relative)) else "other"),
        "sha256": None,
        "git_blob_sha1": None,
        "bytes": None,
        "encoding": None,
        "lfs_pointer": False,
        "pragmas": [],
        "imports": [],
        "errors": ["missing_from_snapshot"],
        "remote_mode": remote.get("mode"),
        "remote_type": remote.get("type"),
        "remote_git_blob_sha1": remote.get("sha"),
    }


def inventory_snapshot(
    source_root: Path,
    output: Path,
    *,
    commit: str,
    source_root_label: str | None = None,
    remote_entries: Mapping[str, Mapping[str, Any]] | None = None,
    remote_metadata: Mapping[str, Any] | None = None,
    extraction_errors: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Inventory every file below ``source_root`` and record all errors."""

    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"source root does not exist: {source_root}")
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = [dict(item) for item in extraction_errors]
    actual_paths: set[str] = set()
    for candidate in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = PurePosixPath(_normalise_relative(candidate.relative_to(source_root)))
        relative_name = relative.as_posix()
        if candidate.is_symlink():
            target = candidate.resolve(strict=False)
            issue = {
                "path": relative_name,
                "error": "symlink_escape" if not _within(target, source_root) else "symlink_not_supported",
                "target": os.readlink(candidate),
            }
            errors.append(issue)
            records.append(
                {
                    "project_id": _project_id(relative),
                    "path": relative_name,
                    "kind": "solidity" if candidate.suffix.lower() == SOLIDITY_SUFFIX else "other",
                    "sha256": None,
                    "git_blob_sha1": None,
                    "bytes": None,
                    "encoding": None,
                    "lfs_pointer": False,
                    "pragmas": [],
                    "imports": [],
                    "errors": [issue["error"]],
                }
            )
            actual_paths.add(relative_name)
            continue
        if not candidate.is_file():
            continue
        if not _within(candidate, source_root):
            errors.append({"path": relative_name, "error": "path_escape"})
            continue
        remote = remote_entries.get(relative_name) if remote_entries is not None else None
        record = _file_record(candidate, source_root, remote=remote)
        records.append(record)
        actual_paths.add(relative_name)
        if record["errors"]:
            errors.extend({"path": relative_name, "error": item} for item in record["errors"])

    if remote_entries is not None:
        for relative_name in sorted(set(remote_entries) - actual_paths):
            record = _missing_record(relative_name, remote_entries[relative_name])
            records.append(record)
            errors.extend({"path": relative_name, "error": item} for item in record["errors"])
        unexpected = sorted(actual_paths - set(remote_entries))
        for relative_name in unexpected:
            errors.append({"path": relative_name, "error": "not_in_remote_tree"})

    records.sort(key=lambda item: item["path"])
    project_rows: dict[str, dict[str, Any]] = {}
    for record in records:
        project = project_rows.setdefault(
            record["project_id"],
            {"project_id": record["project_id"], "file_count": 0, "solidity_file_count": 0, "config_files": [], "errors": []},
        )
        project["file_count"] += 1
        if record["kind"] == "solidity":
            project["solidity_file_count"] += 1
        if record["kind"] == "config":
            project["config_files"].append(record["path"])
        project["errors"].extend(record["errors"])
    projects = sorted(project_rows.values(), key=lambda item: item["project_id"])
    for project in projects:
        project["config_files"].sort()
        project["errors"] = sorted(set(project["errors"]))

    solidity_records = [record for record in records if record["kind"] == "solidity"]
    config_records = [record for record in records if record["kind"] == "config"]
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "source_root": source_root_label or source_root.as_posix(),
        "source_commit": commit,
        "repository": REPOSITORY,
        # ``files`` is the Solidity source inventory consumed by the plan
        # builder. Config/non-source files remain available for provenance.
        "file_count": len(solidity_records),
        "solidity_file_count": len(solidity_records),
        "all_file_count": len(records),
        "config_file_count": len(config_records),
        "project_count": len(projects),
        "projects": projects,
        "files": solidity_records,
        "config_files": config_records,
        "errors": errors,
    }
    if remote_metadata is not None:
        payload["remote_tree"] = dict(remote_metadata)
    _write_json(output, payload)
    return payload


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
    except FileNotFoundError as error:
        raise SnapshotError("git executable is required to verify the complete remote tree") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotError(f"git {' '.join(args)} failed: {detail}") from error
    return result.stdout.decode("utf-8", errors="strict")


def _remote_tree_from_git(
    repository: str,
    commit: str,
    destination: Path,
    *,
    fetch_source: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Fetch commit metadata without blobs and enumerate the complete tree."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and any(destination.iterdir()):
        raise SnapshotError(f"refusing to reuse non-empty remote Git staging path: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], destination)
    try:
        _run_git(
            ["fetch", "--filter=blob:none", "--no-tags", "--depth", "1", fetch_source or repository, commit],
            destination,
        )
    except SnapshotError:
        # Some Git mirrors do not advertise filtering; a normal shallow fetch
        # still gives us the complete tree and is preferable to guessing.
        _run_git(["fetch", "--no-tags", "--depth", "1", fetch_source or repository, commit], destination)
    _run_git(["rev-parse", "--verify", commit], destination)
    # Git for Windows treats the caret/braces expression differently when it
    # arrives through subprocess; the trailing colon is the commit tree.
    tree_sha = _run_git(["rev-parse", f"{commit}:"], destination).strip()
    raw = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--full-tree", commit, "--", CONTRACTS_PREFIX.rstrip("/")],
        cwd=str(destination),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    entries: dict[str, dict[str, Any]] = {}
    prefix = CONTRACTS_PREFIX.encode("utf-8")
    for item in raw.split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode, entry_type, sha = metadata.decode("ascii").split(" ")
        if not raw_path.startswith(prefix):
            continue
        relative = raw_path[len(prefix) :].decode("utf-8", errors="strict")
        entries[relative] = {"mode": mode, "type": entry_type, "sha": sha}
    if not entries:
        raise SnapshotError("remote Git tree contains no contracts entries")
    metadata = {
        "repository": repository,
        "fetch_source": fetch_source or repository,
        "commit": commit,
        "tree_sha": tree_sha,
        "tree_source": "git-ls-tree",
        "tree_truncated": False,
        "entry_count": len(entries),
    }
    return entries, metadata


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part-{os.getpid()}")
    request = urllib.request.Request(url, headers={"User-Agent": "spider-dappscan-inventory/1"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output, length=1 << 20)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, destination)


def _archive_member_relative(name: str, archive_root: str) -> tuple[str | None, str | None]:
    if "\\" in name:
        return None, "backslash_in_archive_path"
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None, "unsafe_archive_path"
    prefix = PurePosixPath(archive_root) / PurePosixPath(CONTRACTS_PREFIX.rstrip("/"))
    try:
        relative = path.relative_to(prefix)
    except ValueError:
        return None, None
    return relative.as_posix(), None


def _extract_contracts(archive: Path, destination: Path) -> list[dict[str, Any]]:
    """Extract only contracts, refusing symlinks, hardlinks, and traversal."""

    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise SnapshotError(f"refusing to extract over non-empty directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, Any]] = []
    with tarfile.open(archive, mode="r:gz") as handle:
        names = [member.name.replace("\\", "\\") for member in handle.getmembers()]
        roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
        if len(roots) != 1:
            raise SnapshotError(f"archive must have one root directory, found {sorted(roots)}")
        archive_root = next(iter(roots))
        seen: set[str] = set()
        for member in handle.getmembers():
            relative, path_error = _archive_member_relative(member.name, archive_root)
            if path_error:
                errors.append({"path": member.name, "error": path_error})
                continue
            if relative is None or relative == ".":
                continue
            if relative in seen:
                errors.append({"path": relative, "error": "duplicate_archive_path"})
                continue
            seen.add(relative)
            target = destination / Path(*PurePosixPath(relative).parts)
            if not _within(target, destination):
                errors.append({"path": relative, "error": "path_escape"})
                continue
            if member.issym() or member.islnk():
                errors.append(
                    {"path": relative, "error": "symlink_or_hardlink_not_supported", "target": member.linkname}
                )
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                errors.append({"path": relative, "error": "unsupported_archive_member"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                errors.append({"path": relative, "error": "duplicate_archive_path"})
                continue
            source = handle.extractfile(member)
            if source is None:
                errors.append({"path": relative, "error": "archive_member_unreadable"})
                continue
            with target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1 << 20)
            try:
                target.chmod(member.mode & 0o777)
            except OSError:
                pass
    return errors


def _promote_snapshot(extracted_root: Path, destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists():
        if any(destination.iterdir()):
            raise SnapshotError(f"refusing to overwrite existing snapshot: {destination}")
        destination.rmdir()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.partial-{os.getpid()}"
    if temporary.exists():
        raise SnapshotError(f"stale partial snapshot exists: {temporary}")
    shutil.copytree(extracted_root, temporary)
    os.replace(temporary, destination)


def fetch_snapshot(
    *,
    repository: str,
    commit: str,
    staging_root: Path,
    snapshot_root: Path,
    inventory_output: Path,
    archive_path: Path | None = None,
    git_source: str | None = None,
) -> dict[str, Any]:
    staging_root = staging_root.resolve()
    if staging_root.exists() and any(staging_root.iterdir()):
        existing = {item.resolve() for item in staging_root.iterdir()}
        allowed = {archive_path.resolve()} if archive_path is not None and archive_path.parent.resolve() == staging_root else set()
        if existing != allowed:
            raise SnapshotError(f"refusing to reuse non-empty staging directory: {staging_root}")
    staging_root.mkdir(parents=True, exist_ok=True)
    archive = (archive_path or (staging_root / f"DAppSCAN-{commit}.tar.gz")).resolve()
    if archive_path is None:
        _download(f"{ARCHIVE_BASE}/{commit}.tar.gz", archive)
    elif not archive.is_file():
        raise FileNotFoundError(f"provided archive does not exist: {archive}")
    remote_entries, remote_metadata = _remote_tree_from_git(
        repository,
        commit,
        staging_root / "remote-git",
        fetch_source=git_source,
    )
    extracted_contracts = staging_root / "extracted" / "DAppSCAN-source" / "contracts"
    extraction_errors = _extract_contracts(archive, extracted_contracts)
    if set(remote_entries) != {
        path.relative_to(extracted_contracts).as_posix()
        for path in extracted_contracts.rglob("*")
        if path.is_file() and not path.is_symlink()
    }:
        extraction_errors.append({"error": "archive_tree_mismatch", "remote_entries": len(remote_entries)})
    # Verify the staging tree before it can graduate. This keeps a partial or
    # hash-mismatched archive out of ``external`` even when extraction itself
    # completed successfully.
    preflight_inventory_path = staging_root / "inventory.preflight.json"
    preflight = inventory_snapshot(
        extracted_contracts,
        preflight_inventory_path,
        commit=commit,
        source_root_label=extracted_contracts.as_posix(),
        remote_entries=remote_entries,
        remote_metadata=remote_metadata,
        extraction_errors=extraction_errors,
    )
    expected_counts = commit == DEFAULT_COMMIT
    if expected_counts and (
        preflight["solidity_file_count"] != EXPECTED_SOLIDITY_FILES
        or preflight["project_count"] != EXPECTED_PROJECTS
    ):
        raise SnapshotError(
            "pinned DAppSCAN count mismatch: "
            f"{preflight['project_count']} projects, {preflight['solidity_file_count']} Solidity files"
        )
    if preflight["errors"]:
        raise SnapshotError(
            f"snapshot verification failed with {len(preflight['errors'])} error(s); "
            f"see {preflight_inventory_path}"
        )
    _promote_snapshot(extracted_contracts.parent.parent, snapshot_root)
    final_contracts = snapshot_root.resolve() / "DAppSCAN-source" / "contracts"
    payload = inventory_snapshot(
        final_contracts,
        inventory_output,
        commit=commit,
        source_root_label=final_contracts.as_posix(),
        remote_entries=remote_entries,
        remote_metadata=remote_metadata,
        extraction_errors=extraction_errors,
    )
    snapshot_manifest = {
        "schema": SNAPSHOT_SCHEMA,
        "source_commit": commit,
        "repository": repository,
        "archive": {"path": archive.as_posix(), "sha256": _sha256(archive)},
        "remote_tree": remote_metadata,
        "inventory": inventory_output.as_posix(),
        "snapshot_root": snapshot_root.as_posix(),
        "inventory_error_count": len(payload["errors"]),
    }
    _write_json(snapshot_root / "snapshot_manifest.json", snapshot_manifest)
    return {"snapshot": snapshot_manifest, "inventory": payload}


def _path(value: str) -> Path:
    return Path(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory", help="inventory an existing contracts source root")
    inventory_parser.add_argument("--source-root", type=_path, required=True)
    inventory_parser.add_argument("--output", type=_path, required=True)
    inventory_parser.add_argument("--commit", required=True)
    inventory_parser.add_argument("--source-root-label")

    snapshot_parser = subparsers.add_parser("snapshot", help="download, verify, promote, and inventory DAppSCAN")
    snapshot_parser.add_argument("--repository", default=REPOSITORY)
    snapshot_parser.add_argument("--commit", default=DEFAULT_COMMIT)
    snapshot_parser.add_argument("--staging-root", type=_path, default=Path("work/spider-dappscan-full"))
    snapshot_parser.add_argument(
        "--snapshot-root", type=_path, default=Path("external/DAppSCAN-full-66a56619")
    )
    snapshot_parser.add_argument(
        "--inventory-output",
        type=_path,
        default=Path("outputs/evaluations/spider-dappscan-full/inventory/dappscan_inventory.json"),
    )
    snapshot_parser.add_argument("--archive", type=_path, help="use a pre-fetched archive instead of downloading")
    snapshot_parser.add_argument("--git-source", help="read the pinned Git tree from a local/alternate remote")
    args = parser.parse_args(argv)
    if args.command == "inventory":
        payload = inventory_snapshot(
            args.source_root,
            args.output,
            commit=args.commit,
            source_root_label=args.source_root_label,
        )
    else:
        result = fetch_snapshot(
            repository=args.repository,
            commit=args.commit,
            staging_root=args.staging_root,
            snapshot_root=args.snapshot_root,
            inventory_output=args.inventory_output,
            archive_path=args.archive,
            git_source=args.git_source,
        )
        payload = {
            "snapshot_root": result["snapshot"]["snapshot_root"],
            "inventory_output": args.inventory_output.as_posix(),
            "project_count": result["inventory"]["project_count"],
            "solidity_file_count": result["inventory"]["solidity_file_count"],
            "error_count": len(result["inventory"]["errors"]),
        }
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if not payload.get("error_count") else 2


if __name__ == "__main__":
    sys.exit(main())
