"""Comment-aware Solidity import scanning and local import closure resolution.

The resolver deliberately works from the compiler's source-unit names rather
than walking every ``.sol`` file in a project.  This keeps dependency
resolution deterministic and avoids accidentally compiling skipped build
directories.  ``resolve_closure`` returns source-unit names suitable for a
Standard JSON ``sources`` mapping and the physical files that provide them.

The caller must pass remappings to solc using the same strings supplied here.
For a remapping such as ``@openzeppelin/contracts/=lib/openzeppelin/contracts/``
the returned dependency key is the remapping target plus the unmatched suffix
(``lib/openzeppelin/contracts/...``).  This mirrors solc's remapped source-unit
name.  Without a remapping, a dependency found below ``node_modules``, ``lib``
or another local dependency root keeps the import string as its source-unit
name; the physical path is still returned for the caller to populate.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

_IDENTIFIER_START = re.compile(r"[A-Za-z_$]")
_IDENTIFIER_CONTINUE = re.compile(r"[A-Za-z0-9_$]")
_DEPENDENCY_ROOT_NAMES = frozenset({"node_modules", "lib", "vendor", "vendors", "deps", "dependencies", "external"})


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


@dataclass(frozen=True)
class _Remapping:
    context: str
    prefix: str
    target: str
    target_path: Path


def _decode_string(value: str) -> str:
    """Decode only Solidity escapes relevant to a path-like string literal."""

    escapes = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "r": "\r", "t": "\t"}
    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            next_char = value[index + 1]
            output.append(escapes.get(next_char, "\\" + next_char))
            index += 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _tokens(source: str) -> Iterator[_Token]:
    """Yield the small lexical subset needed to find Solidity imports.

    Solidity comments and quoted literals are consumed here instead of being
    removed with a regular expression.  That prevents text such as
    ``"import 'fake.sol';"`` from becoming an import edge.
    """

    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        nxt = source[index + 1] if index + 1 < length else ""
        if char.isspace():
            index += 1
            continue
        if char == "/" and nxt == "/":
            index += 2
            while index < length and source[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            end = source.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            value: list[str] = []
            while index < length:
                current = source[index]
                if current == "\\" and index + 1 < length:
                    value.extend((current, source[index + 1]))
                    index += 2
                    continue
                if current == quote:
                    index += 1
                    break
                value.append(current)
                index += 1
            yield _Token("string", _decode_string("".join(value)))
            continue
        if _IDENTIFIER_START.fullmatch(char):
            start = index
            index += 1
            while index < length and _IDENTIFIER_CONTINUE.fullmatch(source[index]):
                index += 1
            yield _Token("identifier", source[start:index])
            continue
        yield _Token("punctuation", char)
        index += 1


def imports(text: str) -> list[str]:
    """Return Solidity import paths in source order.

    Both direct imports and ``import ... from`` forms are supported.  Duplicate
    paths are retained because the result is a lexical scan; closure traversal
    deduplicates source units separately.
    """

    tokens = list(_tokens(text))
    found: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind != "identifier" or token.value != "import":
            index += 1
            continue
        index += 1
        while index < len(tokens):
            token = tokens[index]
            if token.kind == "string":
                found.append(token.value)
                break
            if token.value == ";":
                break
            index += 1
        index += 1
    return found


def _normal_source_name(value: str) -> str:
    value = value.replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise ValueError(f"invalid Solidity source-unit name: {value!r}")
    normalized = posixpath.normpath(value)
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"source-unit path escapes project root: {value!r}")
    return normalized


def _relative_source_name(value: str, importer: str) -> str:
    value = value.replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise ValueError(f"source-unit path is absolute: {value!r}")
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(importer), value))
    if re.match(r"^[A-Za-z]:/", joined) or joined.startswith("/"):
        return joined
    return _normal_source_name(joined)


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def resolve_symlink_payload(path: Path, project: Path) -> Path | None:
    """Resolve a regular-file symlink payload without changing its bytes.

    Git snapshots sometimes contain a symlink target as the complete contents
    of a ``.sol`` regular file.  Treat only an exact single-line relative path
    as this representation.  The target is resolved from the payload file's
    directory and must remain inside ``project``.
    """

    project = Path(project).resolve()
    current = Path(path).resolve(strict=False)
    seen: set[Path] = set()
    while current not in seen:
        seen.add(current)
        if current.suffix.lower() != ".sol" or not current.is_file():
            return None
        try:
            payload = current.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if not payload.startswith(("./", "../")) or any(char in payload for char in "\r\n\x00"):
            return None
        payload = payload.replace("\\", "/")
        target = (current.parent / PurePosixPath(payload)).resolve(strict=False)
        if not _path_inside(target, project):
            raise ValueError(
                f"SYMLINK_PAYLOAD_ESCAPE: source={path!s} payload={payload!r} target={target!s} project={project!s}"
            )
        if target.parent.is_dir():
            matches = [item for item in target.parent.iterdir() if item.name.casefold() == target.name.casefold()]
            if len(matches) > 1:
                raise ValueError(
                    f"SYMLINK_PAYLOAD_AMBIGUOUS: source={path!s} payload={payload!r} candidates={[str(item) for item in matches]}"
                )
        if not target.is_file():
            raise ValueError(
                f"SYMLINK_PAYLOAD_MISSING: source={path!s} payload={payload!r} target={target!s}"
            )
        if target.suffix.lower() != ".sol":
            raise ValueError(
                f"SYMLINK_PAYLOAD_NOT_SOLIDITY: source={path!s} payload={payload!r} target={target!s}"
            )
        if target in seen:
            raise ValueError(
                f"SYMLINK_PAYLOAD_AMBIGUOUS: cyclic regular-file payload source={path!s} target={target!s}"
            )
        next_payload = target.read_bytes()
        try:
            next_text = next_payload.decode("utf-8")
        except UnicodeDecodeError:
            return target
        if not next_text.startswith(("./", "../")) or any(char in next_text for char in "\r\n\x00"):
            return target
        current = target
    raise ValueError(f"SYMLINK_PAYLOAD_AMBIGUOUS: cyclic regular-file payload source={path!s}")


def _format_resolution_error(code: str, importer: str, imported: str, paths: list[Path]) -> ValueError:
    tried = ", ".join(str(path) for path in paths) or "<none>"
    return ValueError(f"{code}: importer={importer!r} import={imported!r} tried=[{tried}]")


def _parse_remappings(project: Path, remappings: list[str]) -> list[_Remapping]:
    parsed: list[_Remapping] = []
    for raw in remappings:
        if "=" not in raw:
            raise ValueError(f"invalid Solidity remapping (expected prefix=target): {raw!r}")
        left, target = raw.split("=", 1)
        if not left or not target:
            raise ValueError(f"invalid Solidity remapping (expected prefix=target): {raw!r}")
        context, separator, prefix = left.rpartition(":")
        if not separator:
            context, prefix = "", left
        target = target.replace("\\", "/")
        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = project / target_path
        parsed.append(
            _Remapping(
                context=context.replace("\\", "/").rstrip("/"),
                prefix=prefix.replace("\\", "/"),
                target=target.rstrip("/"),
                target_path=target_path.resolve(strict=False),
            )
        )
    return parsed


def _matching_remappings(importer: str, imported: str, remappings: list[_Remapping]) -> list[_Remapping]:
    matches = [
        item
        for item in remappings
        if imported.startswith(item.prefix)
        and (not item.context or importer == item.context or importer.startswith(item.context + "/"))
    ]
    if not matches:
        return []
    best = max((len(item.prefix), len(item.context)) for item in matches)
    return [item for item in matches if (len(item.prefix), len(item.context)) == best]


def _remapped_source_name(project: Path, remapping: _Remapping, suffix: str) -> str:
    """Build solc's source-unit key for a remapping target."""

    target_path = remapping.target_path
    if _path_inside(target_path, project):
        target_name = target_path.relative_to(project).as_posix()
    else:
        target_name = target_path.as_posix()
    logical_name = posixpath.join(target_name, suffix.replace("\\", "/").lstrip("/"))
    if re.match(r"^[A-Za-z]:/", logical_name) or logical_name.startswith("/"):
        return posixpath.normpath(logical_name)
    return _normal_source_name(logical_name)


def _dependency_roots(project: Path, current: Path, allowed_roots: list[Path]) -> list[Path]:
    """Return deterministic local dependency roots near the current source."""

    roots: list[Path] = []
    seen: set[Path] = set()
    ancestors: list[Path] = []
    cursor = current.parent.resolve(strict=False)
    while True:
        ancestors.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    for ancestor in ancestors:
        for name in sorted(_DEPENDENCY_ROOT_NAMES):
            candidate = ancestor / name
            if candidate.is_dir() and candidate.resolve(strict=False) not in seen:
                roots.append(candidate)
                seen.add(candidate.resolve(strict=False))
    for root in [project, *allowed_roots]:
        for name in sorted(_DEPENDENCY_ROOT_NAMES):
            candidate = root / name
            if candidate.is_dir() and candidate.resolve(strict=False) not in seen:
                roots.append(candidate)
                seen.add(candidate.resolve(strict=False))
    return roots


def _candidate_file(
    path: Path,
    allowed_roots: list[Path],
    *,
    importer: str,
    imported: str,
    tried: list[Path],
    project: Path | None = None,
) -> Path | None:
    resolved = path.resolve(strict=False)
    tried.append(path)
    if not path.is_file():
        return None
    if not any(_path_inside(resolved, root) for root in allowed_roots):
        raise ValueError(
            f"SOURCE_OUTSIDE_ROOT: importer={importer!r} import={imported!r} tried=[{path}]"
        )
    if project is not None and _path_inside(resolved, project):
        recovered = resolve_symlink_payload(resolved, project)
        if recovered is not None:
            return recovered
    return resolved


def resolve_closure(project: Path, entries: list[str], remappings: list[str] | None = None) -> dict[str, Path]:
    """Resolve recursive Solidity imports without mutating project files.

    ``entries`` are project-relative POSIX source-unit names.  Normal sources
    must resolve beneath ``project`` after symlink resolution.  A remapping may
    explicitly name a target outside the project; that target becomes an
    allowed root for the remapped closure.  Missing and ambiguous imports raise
    ``ValueError`` containing ``MISSING_DEPENDENCY`` or
    ``AMBIGUOUS_DEPENDENCY`` together with importer, import string and tried
    paths.
    """

    project = Path(project).resolve()
    if not project.is_dir():
        raise NotADirectoryError(project)
    parsed_remappings = _parse_remappings(project, list(remappings or []))
    allowed_roots = [project]
    for remapping in parsed_remappings:
        if remapping.target_path.is_dir():
            allowed_roots.append(remapping.target_path)

    pending: list[tuple[str, Path]] = []
    for entry in entries:
        source_name = _normal_source_name(entry)
        path = project / PurePosixPath(source_name)
        resolved = path.resolve(strict=False)
        if not _path_inside(resolved, project):
            raise ValueError(f"SOURCE_OUTSIDE_ROOT: importer='<entry>' import={entry!r} tried=[{path}]")
        if not path.is_file():
            raise _format_resolution_error("MISSING_DEPENDENCY", "<entry>", entry, [path])
        recovered = resolve_symlink_payload(path, project)
        if recovered is not None:
            resolved = recovered
        pending.append((source_name, resolved))

    resolved_sources: dict[str, Path] = {}
    while pending:
        source_name, source_path = pending.pop()
        previous = resolved_sources.get(source_name)
        if previous is not None:
            if previous != source_path:
                raise _format_resolution_error("AMBIGUOUS_DEPENDENCY", source_name, source_name, [previous, source_path])
            continue
        resolved_sources[source_name] = source_path
        text = source_path.read_text(encoding="utf-8", errors="strict")
        for imported in imports(text):
            imported = imported.replace("\\", "/")
            relative_import = imported.startswith("./") or imported.startswith("../") or imported in {".", ".."}
            logical_import = _relative_source_name(imported, source_name) if relative_import else _normal_source_name(imported)
            # solc normalizes a relative import against the importing source
            # unit before it applies remappings (e.g. contracts/../lib/X.sol).
            remap_matches = _matching_remappings(source_name, logical_import, parsed_remappings)
            tried: list[Path] = []
            candidates: list[tuple[str, Path]] = []
            if remap_matches:
                remap_paths: list[Path] = []
                for remapping in remap_matches:
                    suffix = logical_import[len(remapping.prefix) :]
                    suffix = suffix.lstrip("/")
                    try:
                        logical_name = _remapped_source_name(project, remapping, suffix)
                    except ValueError:
                        continue
                    candidate = remapping.target_path / PurePosixPath(suffix)
                    remap_paths.append(candidate)
                    resolved = _candidate_file(
                        candidate,
                        [*allowed_roots, remapping.target_path],
                        importer=source_name,
                        imported=imported,
                        tried=tried,
                        project=project,
                    )
                    if resolved is not None:
                        candidates.append((logical_name, resolved))
                if not candidates and remap_paths:
                    raise _format_resolution_error("MISSING_DEPENDENCY", source_name, imported, tried)
            else:
                if relative_import:
                    logical_name = logical_import
                    logical_path = project / PurePosixPath(source_name)
                    source_parent = (
                        logical_path.parent
                        if logical_path.is_file() and _path_inside(logical_path, project)
                        else source_path.parent
                    )
                    candidate = source_parent / PurePosixPath(imported)
                    resolved = _candidate_file(
                        candidate,
                        allowed_roots,
                        importer=source_name,
                        imported=imported,
                        tried=tried,
                        project=project,
                    )
                    if resolved is not None:
                        candidates.append((logical_name, resolved))
                else:
                    logical_name = _normal_source_name(imported)
                    direct = project / PurePosixPath(logical_name)
                    resolved = _candidate_file(
                        direct,
                        allowed_roots,
                        importer=source_name,
                        imported=imported,
                        tried=tried,
                        project=project,
                    )
                    if resolved is not None:
                        candidates.append((logical_name, resolved))
                    else:
                        for root in _dependency_roots(project, source_path, [*allowed_roots, project]):
                            resolved = _candidate_file(
                                root / PurePosixPath(logical_name),
                                allowed_roots,
                                importer=source_name,
                                imported=imported,
                                tried=tried,
                                project=project,
                            )
                            if resolved is not None:
                                candidates.append((logical_name, resolved))
            unique = {(name, path) for name, path in candidates}
            if len(unique) > 1:
                raise _format_resolution_error(
                    "AMBIGUOUS_DEPENDENCY", source_name, imported, [path for _, path in sorted(unique, key=lambda item: str(item[1]))]
                )
            if not candidates:
                raise _format_resolution_error("MISSING_DEPENDENCY", source_name, imported, tried)
            pending.append(candidates[0])
    return dict(sorted(resolved_sources.items()))


__all__ = ["imports", "resolve_closure", "resolve_symlink_payload"]
