from __future__ import annotations

import hashlib
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from solc_select.solc_select import artifact_path, installed_versions

PRAGMA_RE = re.compile(r"\bpragma\s+solidity\s*([^;]+);", re.I)
TOKEN_RE = re.compile(r"(\^|>=|<=|>|<|=)?\s*(\d+)\s*\.\s*(\d+)(?:\s*\.\s*(\d+))?")
VERSION_RE = re.compile(r"\bVersion:\s*(\d+\.\d+\.\d+)(\S*)")
_IGNORED_PROJECT_DIRS = {".git", ".hg", ".svn", ".venv", "artifacts", "build", "cache", "node_modules", "out", "venv"}

# Solidity 0.4.15's official Windows release reports its historical nightly
# build label even though the release artifact is pinned and checksummed.  A
# digest match is required before accepting that one upstream quirk.
_AUTHENTICATED_RELEASE_DIGESTS = {
    "0.4.15": {"9e22db7ba9881a4fa43ee38404f4aba531f7e4b84a8ecf5e9e423062e45564fe"},
}
_SOLC_0415_LINUX_ENV = "SPIDER_SOLC_0415_LINUX"


def _without_comments(source: str) -> str:
    """Remove Solidity comments while preserving quoted text and line boundaries."""
    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if quote:
            output.append(char)
            if char == "\\" and index + 1 < len(source):
                output.append(source[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            output.append(char)
            index += 1
            continue
        if char == "/" and nxt == "/":
            end = source.find("\n", index + 2)
            if end < 0:
                break
            output.append("\n")
            index = end + 1
            continue
        if char == "/" and nxt == "*":
            end = source.find("*/", index + 2)
            comment = source[index + 2 :] if end < 0 else source[index + 2 : end]
            output.extend("\n" for item in comment if item == "\n")
            index = len(source) if end < 0 else end + 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def pragma_expressions(path: str | Path) -> list[str]:
    """Return every Solidity pragma expression in source order.

    Solidity permits multiple pragmas in one source unit (including imported
    snippets concatenated into a single file).  Compiler selection must honor
    all of them; comments and string literals are removed only for scanning.
    """
    text = _without_comments(Path(path).read_text(encoding="utf-8", errors="replace"))
    return [match.group(1).strip() for match in PRAGMA_RE.finditer(text)]


def pragma_from(path: str | Path) -> str:
    expressions = pragma_expressions(path)
    return expressions[0] if expressions else ""


def solidity_sources(path: str | Path) -> list[Path]:
    """Return deterministic Solidity inputs for a file or plain project directory."""
    source = Path(path).resolve()
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise FileNotFoundError(source)
    files: list[Path] = []
    for root, directories, filenames in os.walk(source):
        directories[:] = sorted(name for name in directories if name not in _IGNORED_PROJECT_DIRS)
        files.extend(Path(root, name).resolve() for name in sorted(filenames) if Path(name).suffix.lower() == ".sol")
    if not files:
        raise ValueError(f"No Solidity sources found under project directory: {source}")
    return sorted(files, key=lambda item: item.as_posix())


def _matches(version: tuple[int, int, int], expression: str) -> bool:
    position = 0
    found = False
    for token in TOKEN_RE.finditer(expression):
        if expression[position : token.start()].strip():
            return False
        found = True
        operator, major, minor, patch = token.groups()
        other = (int(major), int(minor), int(patch or 0))
        if operator == "^":
            upper = (other[0] + 1, 0, 0) if other[0] else ((0, other[1] + 1, 0) if other[1] else (0, 0, other[2] + 1))
            valid = other <= version < upper
        elif operator in {">=", ">", "<=", "<", "="}:
            valid = {">=": version >= other, ">": version > other, "<=": version <= other, "<": version < other, "=": version == other}[operator]
        elif patch is None:
            valid = other <= version < (other[0], other[1] + 1, 0)
        else:
            valid = version == other
        if not valid:
            return False
        position = token.end()
    return found and not expression[position:].strip()


def compatible_version(expression: str, versions: list[tuple[int, int, int]]) -> str | None:
    candidates = compatible_versions(expression, versions)
    return candidates[0] if candidates else None


def compatible_versions(expression: str, versions: list[tuple[int, int, int]]) -> list[str]:
    matches = [version for version in versions if any(_matches(version, branch.strip()) for branch in expression.split("||"))]
    # Solidity minor releases before 1.0 may be breaking: try the earliest
    # compatible minor family first, but its newest patch first.
    matches.sort(key=lambda version: (version[0], version[1], -version[2]))
    return [".".join(map(str, version)) for version in matches]


def compatible_project_versions(expressions: list[str], versions: list[tuple[int, int, int]]) -> list[str]:
    """Return versions satisfying every non-empty pragma in one compilation unit."""
    constraints = [expression for expression in expressions if expression]
    matches = [
        version
        for version in versions
        if all(any(_matches(version, branch.strip()) for branch in expression.split("||")) for expression in constraints)
    ]
    matches.sort(key=lambda version: (version[0], version[1], -version[2]))
    return [".".join(map(str, version)) for version in matches]


@lru_cache(maxsize=None)
def compiler_fingerprint(requested: str) -> dict[str, str | bool]:
    override = _release_override(requested)
    binary = override or artifact_path(requested)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest() if binary.is_file() else ""
    try:
        result = subprocess.run(compiler_command(requested, "--version"), capture_output=True, text=True, timeout=30)
        match = VERSION_RE.search(result.stdout + result.stderr)
    except (OSError, subprocess.SubprocessError):
        match = None
    reported = "" if match is None else "".join(match.groups())
    release = bool(match and result.returncode == 0 and match.group(1) == requested and not match.group(2).startswith("-"))
    authenticated_release = bool(
        match
        and result.returncode == 0
        and match.group(1) == requested
        and digest in _AUTHENTICATED_RELEASE_DIGESTS.get(requested, set())
    )
    fingerprint = {
        "requested": requested,
        "reported": reported,
        "binary_sha256": digest,
        "usable": release or authenticated_release,
    }
    if override is not None:
        fingerprint["transport"] = "wsl" if os.name == "nt" else "native"
    return fingerprint


def _release_override(requested: str) -> Path | None:
    """Return a pinned alternate release binary when explicitly configured."""

    if requested != "0.4.15":
        return None
    value = os.environ.get(_SOLC_0415_LINUX_ENV, "").strip()
    if not value or (os.name == "nt" and value.startswith("/")):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return None
    path = path.resolve()
    return path if path.is_file() else None


def _windows_to_wsl(path: Path) -> str:
    drive = path.drive[:1].lower()
    if not drive:
        raise ValueError(f"WSL compiler path must be on a Windows drive: {path}")
    relative = path.relative_to(Path(path.anchor))
    return "/mnt/" + drive + "/" + "/".join(relative.parts)


def compiler_command(requested: str, *arguments: str) -> list[str]:
    """Build the pinned compiler command, including the explicit WSL release transport."""

    override = _release_override(requested)
    if override is not None and os.name == "nt":
        return ["wsl.exe", "--", _windows_to_wsl(override), *arguments]
    return [str(override or artifact_path(requested)), *arguments]


def compiler_fingerprints() -> list[dict[str, str | bool]]:
    return [dict(compiler_fingerprint(version)) for version in sorted(installed_versions(), key=lambda item: tuple(map(int, item.split("."))))]


def installed_solc_versions() -> list[tuple[int, int, int]]:
    return [tuple(map(int, item["requested"].split("."))) for item in compiler_fingerprints() if item["usable"]]


def resolve_solc(path: str | Path, version: str | None = None) -> tuple[str, Path]:
    return solc_candidates(path, version)[0]


def solc_candidates(path: str | Path, version: str | None = None) -> list[tuple[str, Path]]:
    source_files = solidity_sources(path)
    expressions = [expression for source in source_files for expression in pragma_expressions(source)]
    constraints = sorted(set(expression for expression in expressions if expression))
    versions = installed_solc_versions() if version is None else []
    selected = [version] if version else (compatible_project_versions(constraints, versions) if constraints else [".".join(map(str, item)) for item in reversed(versions)])
    if not selected:
        detail = (f"project pragmas {constraints!r}" if len(source_files) > 1 else f"pragma {constraints[0]!r}") if constraints else "missing pragma"
        raise ValueError(f"No installed solc satisfies {detail}")
    candidates: list[tuple[str, Path]] = []
    for item in selected:
        fingerprint = compiler_fingerprint(item)
        if not fingerprint["binary_sha256"]:
            raise ValueError(f"solc {item} is not installed; run `solc-select install {item}`")
        if not fingerprint["usable"]:
            raise ValueError(f"solc {item} reports non-release version {fingerprint['reported']!r}")
        candidates.append((item, artifact_path(item)))
    return candidates
