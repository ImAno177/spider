"""Materialize the complete pinned DAppSCAN source from an existing Git object store.

This is the offline equivalent of the archive fetch. It is safe to use when a
shallow/partial checkout has all commit objects but only a small working tree.
The checked-out files are never trusted: ``git archive`` supplies the bytes and
the resulting tree is verified against ``git ls-tree`` before promotion.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from spider_dappscan_inventory import (
    DEFAULT_COMMIT,
    EXPECTED_PROJECTS,
    EXPECTED_SOLIDITY_FILES,
    REPOSITORY,
    SnapshotError,
    _extract_contracts,
    _promote_snapshot,
    _sha256,
    _write_json,
    inventory_snapshot,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return result.stdout.decode("utf-8", errors="strict")


def _tree(repo: Path, commit: str) -> tuple[dict[str, dict[str, str]], str]:
    raw = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", commit, "--", "DAppSCAN-source/contracts"],
        check=True,
        capture_output=True,
    ).stdout
    prefix = b"DAppSCAN-source/contracts/"
    entries: dict[str, dict[str, str]] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode, entry_type, sha = metadata.decode("ascii").split(" ")
        if raw_path.startswith(prefix):
            entries[raw_path[len(prefix) :].decode("utf-8")] = {"mode": mode, "type": entry_type, "sha": sha}
    if not entries:
        raise SnapshotError("local Git tree has no DAppSCAN contracts")
    tree_sha = _git(repo, "rev-parse", f"{commit}:").strip()
    return entries, tree_sha


def fetch_from_git(repo: Path, *, commit: str, staging_root: Path, snapshot_root: Path, inventory_output: Path) -> dict:
    repo, staging_root, snapshot_root, inventory_output = (Path(p).resolve() for p in (repo, staging_root, snapshot_root, inventory_output))
    if _git(repo, "rev-parse", "--is-inside-work-tree").strip() != "true":
        raise SnapshotError(f"not a Git working tree: {repo}")
    if _git(repo, "config", "--get", "remote.origin.url").strip().rstrip("/") != REPOSITORY.rstrip("/"):
        raise SnapshotError("local Git origin is not the pinned DAppSCAN repository")
    _git(repo, "rev-parse", "--verify", commit)
    if staging_root.exists() and any(staging_root.iterdir()):
        raise SnapshotError(f"refusing to reuse non-empty staging directory: {staging_root}")
    staging_root.mkdir(parents=True, exist_ok=True)
    archive = staging_root / f"DAppSCAN-{commit}.tar.gz"
    subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar.gz", f"--prefix=DAppSCAN-{commit}/", f"--output={archive}", commit, "--", "DAppSCAN-source/contracts"],
        check=True,
        capture_output=True,
    )
    remote_entries, tree_sha = _tree(repo, commit)
    extracted = staging_root / "extracted" / "DAppSCAN-source" / "contracts"
    extraction_errors = _extract_contracts(archive, extracted)
    actual_paths = {path.relative_to(extracted).as_posix() for path in extracted.rglob("*") if path.is_file() and not path.is_symlink()}
    if actual_paths != set(remote_entries):
        extraction_errors.append({"error": "archive_tree_mismatch", "remote_entries": len(remote_entries), "actual_entries": len(actual_paths)})
    preflight_path = staging_root / "inventory.preflight.json"
    preflight = inventory_snapshot(
        extracted,
        preflight_path,
        commit=commit,
        source_root_label=extracted.as_posix(),
        remote_entries=remote_entries,
        remote_metadata={"repository": REPOSITORY, "commit": commit, "tree_sha": tree_sha, "tree_source": "local-git-ls-tree", "tree_truncated": False, "entry_count": len(remote_entries)},
        extraction_errors=extraction_errors,
    )
    if commit == DEFAULT_COMMIT and (preflight["project_count"] != EXPECTED_PROJECTS or preflight["solidity_file_count"] != EXPECTED_SOLIDITY_FILES):
        raise SnapshotError(f"pinned DAppSCAN count mismatch: {preflight['project_count']} projects, {preflight['solidity_file_count']} Solidity files")
    if preflight["errors"]:
        raise SnapshotError(f"snapshot verification failed with {len(preflight['errors'])} error(s); see {preflight_path}")
    _promote_snapshot(extracted.parent.parent, snapshot_root)
    final_contracts = snapshot_root / "DAppSCAN-source" / "contracts"
    payload = inventory_snapshot(final_contracts, inventory_output, commit=commit, source_root_label=final_contracts.as_posix(), remote_entries=remote_entries, remote_metadata={"repository": REPOSITORY, "commit": commit, "tree_sha": tree_sha, "tree_source": "local-git-ls-tree", "tree_truncated": False, "entry_count": len(remote_entries)})
    snapshot_manifest = {"schema": "spider-dappscan-snapshot/1", "source_commit": commit, "repository": REPOSITORY, "archive": {"path": archive.as_posix(), "sha256": _sha256(archive)}, "remote_tree": {"tree_sha": tree_sha, "tree_source": "local-git-ls-tree", "tree_truncated": False, "entry_count": len(remote_entries)}, "inventory": inventory_output.as_posix(), "snapshot_root": snapshot_root.as_posix(), "inventory_error_count": len(payload["errors"]), "source_archive": "git-archive-from-verified-local-object-store"}
    _write_json(snapshot_root / "snapshot_manifest.json", snapshot_manifest)
    return {"snapshot": snapshot_manifest, "inventory": payload}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("external/DAppSCAN"))
    parser.add_argument("--commit", default=DEFAULT_COMMIT)
    parser.add_argument("--staging-root", type=Path, default=Path("work/spider-dappscan-full-localgit"))
    parser.add_argument("--snapshot-root", type=Path, default=Path("external/DAppSCAN-full-66a56619"))
    parser.add_argument("--inventory-output", type=Path, default=Path("outputs/evaluations/spider-dappscan-full/inventory/dappscan_inventory.json"))
    args = parser.parse_args()
    result = fetch_from_git(args.repo, commit=args.commit, staging_root=args.staging_root, snapshot_root=args.snapshot_root, inventory_output=args.inventory_output)
    print(json.dumps({"snapshot_root": result["snapshot"]["snapshot_root"], "inventory": result["snapshot"]["inventory"], "project_count": result["inventory"]["project_count"], "solidity_file_count": result["inventory"]["solidity_file_count"], "error_count": len(result["inventory"]["errors"])}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
