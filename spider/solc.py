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
CONSTRUCTOR_RE = re.compile(r"\bconstructor\s*\(")
FALLBACK_SOLC: tuple[str, ...] = ("0.4.26", "0.5.17", "0.6.12", "0.7.6", "0.8.20")
CONSTRUCTOR_MIN: tuple[int, int, int] = (0, 4, 22)
_IGNORED_PROJECT_DIRS = {".git", ".hg", ".svn", ".venv", "artifacts", "build", "cache", "node_modules", "out", "venv"}


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


def pragma_from(path: str | Path) -> str:
    match = PRAGMA_RE.search(_without_comments(Path(path).read_text(encoding="utf-8", errors="replace")))
    return match.group(1).strip() if match else ""


def _without_strings(source: str) -> str:
    """Blank quoted string contents so pragma matches ignore string literals."""
    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\" and index + 1 < len(source):
                output.append("  ")
                index += 2
                continue
            output.append("\n" if char == "\n" else " ")
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            output.append(" ")
            index += 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def pragmas_from(path: str | Path) -> list[str]:
    """Return every `pragma solidity` expression in source order after comment stripping."""
    text = _without_strings(_without_comments(Path(path).read_text(encoding="utf-8", errors="replace")))
    return [match.group(1).strip() for match in PRAGMA_RE.finditer(text)]


def has_constructor_keyword(source: str) -> bool:
    return bool(CONSTRUCTOR_RE.search(_without_comments(source)))


def _blank_non_code(source: str) -> str:
    """Replace comments and string literals with spaces, keeping length and newlines."""
    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if quote:
            if char == "\\" and index + 1 < len(source):
                output.append("  ")
                index += 2
                continue
            output.append("\n" if char == "\n" else " ")
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            output.append(" ")
            index += 1
            continue
        if char == "/" and nxt == "/":
            end = source.find("\n", index + 2)
            if end < 0:
                output.append(" " * (len(source) - index))
                break
            output.append(" " * (end - index) + "\n")
            index = end + 1
            continue
        if char == "/" and nxt == "*":
            end = source.find("*/", index + 2)
            chunk = source[index:] if end < 0 else source[index : end + 2]
            output.append("".join("\n" if item == "\n" else " " for item in chunk))
            index = len(source) if end < 0 else end + 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def first_solidity_segment(source: str) -> str:
    """Keep source up to (not including) a second `pragma solidity`; BCCC concatenations."""
    matches = list(PRAGMA_RE.finditer(_blank_non_code(source)))
    if len(matches) <= 1:
        return source
    return source[: matches[1].start()]


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
    matches.sort(key=_version_key)
    return [".".join(map(str, version)) for version in matches]


def compatible_project_versions(expressions: list[str], versions: list[tuple[int, int, int]]) -> list[str]:
    """Return versions satisfying every non-empty pragma in one compilation unit."""
    constraints = [expression for expression in expressions if expression]
    matches = [
        version
        for version in versions
        if all(any(_matches(version, branch.strip()) for branch in expression.split("||")) for expression in constraints)
    ]
    matches.sort(key=_version_key)
    return [".".join(map(str, version)) for version in matches]


def _version_key(version: tuple[int, int, int]) -> tuple[int, int, int]:
    return (version[0], version[1], -version[2])


def _fmt_version(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def _parse_version(value: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in value.split("."))
    return major, minor, patch


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def expand_candidates(pragma_lists: list[list[str]], versions: list[tuple[int, int, int]], uses_constructor: bool) -> list[str]:
    """Return a deterministic, de-duplicated solc try-order for a compilation unit."""
    installed = {_fmt_version(version) for version in versions}
    expressions = [expression.strip() for group in pragma_lists for expression in group if expression and expression.strip()]
    if not expressions:
        newest = [_fmt_version(version) for version in sorted(versions, reverse=True)]
        return _unique(newest + [item for item in FALLBACK_SOLC if item in installed])

    intersection = compatible_project_versions(expressions, versions)
    if uses_constructor:
        step1_ok = [item for item in intersection if _parse_version(item) >= CONSTRUCTOR_MIN]
        step1_rest = [item for item in intersection if _parse_version(item) < CONSTRUCTOR_MIN]
    else:
        step1_ok = intersection
        step1_rest = []

    step2: list[str] = []
    if uses_constructor:
        era = [version for version in versions if CONSTRUCTOR_MIN <= version < (0, 5, 0)]
        era.sort(key=_version_key)
        step2 = [_fmt_version(version) for version in era]
        step2.extend(item for item in FALLBACK_SOLC if item in installed)

    distinct = sorted(set(expressions))
    step3: list[str] = []
    for expression in distinct:
        step3.extend(compatible_versions(expression, versions))

    step4: list[str] = []
    for expression in distinct:
        token = TOKEN_RE.search(expression)
        if token is None:
            continue
        family = (int(token.group(2)), int(token.group(3)))
        family_versions = [version for version in versions if version[:2] == family]
        family_versions.sort(key=_version_key)
        step4.extend(_fmt_version(version) for version in family_versions)

    step5 = [item for item in FALLBACK_SOLC if item in installed]
    return _unique(step1_ok + step2 + step1_rest + step3 + step4 + step5)


@lru_cache(maxsize=None)
def compiler_fingerprint(requested: str) -> dict[str, str | bool]:
    binary = artifact_path(requested)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest() if binary.is_file() else ""
    try:
        result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30)
        match = VERSION_RE.search(result.stdout + result.stderr)
    except (OSError, subprocess.SubprocessError):
        match = None
    reported = "" if match is None else "".join(match.groups())
    return {
        "requested": requested,
        "reported": reported,
        "binary_sha256": digest,
        "usable": bool(match and result.returncode == 0 and match.group(1) == requested and not match.group(2).startswith("-")),
    }


def compiler_fingerprints() -> list[dict[str, str | bool]]:
    return [dict(compiler_fingerprint(version)) for version in sorted(installed_versions(), key=lambda item: tuple(map(int, item.split("."))))]


def installed_solc_versions() -> list[tuple[int, int, int]]:
    return [tuple(map(int, item["requested"].split("."))) for item in compiler_fingerprints() if item["usable"]]


def resolve_solc(path: str | Path, version: str | None = None) -> tuple[str, Path]:
    return solc_candidates(path, version)[0]


def solc_candidates(path: str | Path, version: str | None = None, *, expand: bool = True) -> list[tuple[str, Path]]:
    source_files = solidity_sources(path)
    expressions = [pragma_from(source) for source in source_files]
    constraints = sorted(set(expression for expression in expressions if expression))

    def unsatisfied() -> None:
        detail = (f"project pragmas {constraints!r}" if len(source_files) > 1 else f"pragma {constraints[0]!r}") if constraints else "missing pragma"
        raise ValueError(f"No installed solc satisfies {detail}")

    skip_unusable = False
    if version is not None:
        selected = [version]
    elif not expand:
        versions = installed_solc_versions()
        selected = compatible_project_versions(constraints, versions) if constraints else [".".join(map(str, item)) for item in reversed(versions)]
        if not selected:
            unsatisfied()
    else:
        versions = installed_solc_versions()
        pragma_lists = [pragmas_from(source) for source in source_files]
        uses_constructor = any(has_constructor_keyword(source.read_text(encoding="utf-8", errors="replace")) for source in source_files)
        selected = expand_candidates(pragma_lists, versions, uses_constructor)
        skip_unusable = True
        if not selected:
            unsatisfied()

    candidates: list[tuple[str, Path]] = []
    for item in selected:
        fingerprint = compiler_fingerprint(item)
        if not fingerprint["binary_sha256"]:
            if skip_unusable:
                continue
            raise ValueError(f"solc {item} is not installed; run `solc-select install {item}`")
        if not fingerprint["usable"]:
            if skip_unusable:
                continue
            raise ValueError(f"solc {item} reports non-release version {fingerprint['reported']!r}")
        candidates.append((item, artifact_path(item)))
    if not candidates:
        unsatisfied()
    return candidates
