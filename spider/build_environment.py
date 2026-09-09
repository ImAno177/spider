"""Read-only discovery of compiler settings from reproducible project evidence.

This module never executes JavaScript/TypeScript configuration and never
downloads dependencies.  A build-info document is accepted only when its
Standard JSON input contains byte-for-byte source contents that match files in
the supplied project.  Foundry TOML and ``remappings.txt`` are configuration
evidence, not proof that a previous build succeeded.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - used only on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+)(?:[+\-].*)?$")
_DYNAMIC_CONFIGS = (
    "hardhat.config.js",
    "hardhat.config.cjs",
    "hardhat.config.mjs",
    "hardhat.config.ts",
    "truffle-config.js",
    "truffle-config.cjs",
    "truffle-config.mjs",
    "truffle-config.ts",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_copy(value: Any) -> Any:
    """Copy JSON-shaped configuration without retaining caller-owned objects."""

    return copy.deepcopy(value)


def _safe_source_path(project: Path, source_name: str) -> tuple[str, Path] | None:
    if not isinstance(source_name, str) or not source_name or "\\" in source_name or ":" in source_name:
        return None
    relative = PurePosixPath(source_name)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    normalized = relative.as_posix()
    if normalized in {"", "."}:
        return None
    path = (project / Path(*relative.parts)).resolve(strict=False)
    try:
        path.relative_to(project.resolve())
    except ValueError:
        return None
    return normalized, path


def _normal_version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.fullmatch(value.strip())
    return match.group(1) if match else None


def _compiler_version(document: dict[str, Any]) -> str | None:
    for key in ("solcVersion", "compilerVersion", "solc"):
        value = document.get(key)
        if isinstance(value, str):
            version = _normal_version(value)
            if version:
                return version
    compiler = document.get("compiler")
    if isinstance(compiler, str):
        return _normal_version(compiler)
    if isinstance(compiler, dict):
        for key in ("version", "solcVersion", "longVersion"):
            version = _normal_version(compiler.get(key))
            if version:
                return version
    return None


def _settings_fingerprint(settings: dict[str, Any], compiler_version: str | None, remappings: list[str]) -> str:
    value = {"compiler_version": compiler_version, "settings": settings, "remappings": remappings}
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _verify_build_sources(project: Path, sources: Any) -> tuple[bool, list[str], list[str]]:
    """Return validity, normalized source names, and diagnostics."""

    if not isinstance(sources, dict) or not sources:
        return False, [], ["build-info input has no Solidity sources"]
    names: list[str] = []
    diagnostics: list[str] = []
    for name, record in sorted(sources.items(), key=lambda item: str(item[0])):
        safe = _safe_source_path(project, name)
        if safe is None:
            diagnostics.append(f"unsafe or external build-info source path: {name!r}")
            continue
        normalized, path = safe
        if not isinstance(record, dict) or not isinstance(record.get("content"), str):
            diagnostics.append(f"build-info source has no embedded content: {name!r}")
            continue
        if not path.is_file():
            diagnostics.append(f"build-info source is absent from project: {name!r}")
            continue
        try:
            actual = path.read_bytes()
        except OSError as error:
            diagnostics.append(f"cannot read build-info source {name!r}: {error}")
            continue
        expected = record["content"].encode("utf-8")
        if actual != expected:
            diagnostics.append(f"stale build-info source content mismatch: {name!r}")
            continue
        names.append(normalized)
    return not diagnostics and bool(names), names, diagnostics


def _build_info_candidate(project: Path, path: Path, origin: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return None, f"cannot read build-info {path}: {error}"
    if not isinstance(document, dict):
        return None, f"build-info is not an object: {path}"
    standard_input = document.get("input")
    standard_output = document.get("output")
    if not isinstance(standard_input, dict) or not isinstance(standard_output, dict):
        return None, f"build-info lacks standard input/output: {path}"
    if standard_input.get("language", "Solidity") != "Solidity":
        return None, f"build-info is not Solidity: {path}"
    settings = standard_input.get("settings")
    if not isinstance(settings, dict):
        return None, f"build-info has invalid settings: {path}"
    valid, source_names, diagnostics = _verify_build_sources(project, standard_input.get("sources"))
    if not valid:
        return None, f"{path}: " + "; ".join(diagnostics)
    compiler_version = _compiler_version(document)
    if compiler_version is None:
        return None, f"build-info has no stable compiler version: {path}"
    remappings = settings.get("remappings", [])
    if not isinstance(remappings, list) or not all(isinstance(item, str) for item in remappings):
        return None, f"build-info has invalid remappings: {path}"
    evidence = {
        "kind": origin,
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "sources": source_names,
    }
    return (
        {
            "settings": _json_copy(settings),
            "compiler_version": compiler_version,
            "remappings": list(remappings),
            "origin": origin,
            "evidence": [evidence],
            "warnings": [],
        },
        None,
    )


def _build_info_paths(project: Path) -> list[tuple[Path, str]]:
    locations = (
        (project / "artifacts" / "build-info", "hardhat-build-info"),
        (project / "out" / "build-info", "foundry-build-info"),
    )
    paths: list[tuple[Path, str]] = []
    for directory, origin in locations:
        if directory.is_dir():
            paths.extend((path, origin) for path in sorted(directory.glob("*.json")) if path.is_file())
    return paths


def _read_remappings(path: Path) -> tuple[list[str], list[str]]:
    remappings: list[str] = []
    warnings: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        return [], [f"cannot read remappings.txt: {error}"]
    for line_number, line in enumerate(lines, 1):
        value = line.strip()
        if not value or value.startswith("#") or value.startswith("//"):
            continue
        if "=" not in value or value.startswith("=") or value.endswith("="):
            warnings.append(f"invalid remappings.txt entry at line {line_number}")
            continue
        remappings.append(value)
    return remappings, warnings


def _foundry_settings(project: Path, path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        return None, [f"cannot parse foundry.toml: {error}"]
    if not isinstance(data, dict):
        return None, ["foundry.toml is not an object"]
    profile = data.get("profile", {})
    selected = profile.get("default", {}) if isinstance(profile, dict) else {}
    if not isinstance(selected, dict):
        selected = {}
    # A few old Foundry files keep these keys at top level; profile.default
    # remains authoritative when both forms are present.
    config = {key: value for key, value in data.items() if key != "profile"}
    config.update(selected)
    settings: dict[str, Any] = {}
    optimizer = config.get("optimizer")
    if isinstance(optimizer, bool):
        settings["optimizer"] = {"enabled": optimizer}
    elif optimizer is not None:
        warnings.append("foundry optimizer setting is not boolean")
    if "optimizer_runs" in config:
        runs = config["optimizer_runs"]
        if isinstance(runs, int) and not isinstance(runs, bool):
            settings.setdefault("optimizer", {})["runs"] = runs
        else:
            warnings.append("foundry optimizer_runs setting is not an integer")
    if isinstance(config.get("evm_version"), str):
        settings["evmVersion"] = config["evm_version"]
    if isinstance(config.get("via_ir"), bool):
        settings["viaIR"] = config["via_ir"]
    if isinstance(config.get("libraries"), dict):
        settings["libraries"] = _json_copy(config["libraries"])
    metadata: dict[str, Any] = {}
    if isinstance(config.get("bytecode_hash"), str):
        metadata["bytecodeHash"] = config["bytecode_hash"]
    if isinstance(config.get("use_literal_content"), bool):
        metadata["useLiteralContent"] = config["use_literal_content"]
    if metadata:
        settings["metadata"] = metadata
    version = _normal_version(config.get("solc_version")) or _normal_version(config.get("solc"))
    if version is None:
        warnings.append("foundry.toml does not pin a stable compiler version")
    remappings = config.get("remappings")
    if remappings is not None and (not isinstance(remappings, list) or not all(isinstance(item, str) for item in remappings)):
        warnings.append("foundry remappings setting is invalid")
        remappings = []
    result = {
        "settings": settings,
        "compiler_version": version,
        "remappings": list(remappings or []),
        "origin": "foundry.toml",
        "evidence": [{"kind": "foundry.toml", "path": str(path.resolve()), "sha256": _sha256(path)}],
        "warnings": warnings,
    }
    return result, warnings


def _fallback(project: Path, warnings: list[str], remappings: list[str], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    if not warnings:
        warnings = ["no verified build settings found; using source-closure defaults"]
    return {
        "settings": {"remappings": list(remappings)} if remappings else {},
        "compiler_version": None,
        "remappings": list(remappings),
        "origin": "reconstructed-fallback",
        "evidence": evidence,
        "warnings": warnings,
    }


def _override_result(override: dict[str, Any]) -> dict[str, Any]:
    required = ("settings", "compiler_version", "remappings", "evidence")
    missing = [key for key in required if key not in override]
    if missing:
        raise ValueError(f"invalid build override: missing {', '.join(missing)}")
    if not isinstance(override["settings"], dict):
        raise ValueError("invalid build override: settings must be an object")
    if override["compiler_version"] is not None and _normal_version(override["compiler_version"]) is None:
        raise ValueError("invalid build override: compiler_version must be a stable x.y.z version")
    if not isinstance(override["remappings"], list) or not all(isinstance(item, str) for item in override["remappings"]):
        raise ValueError("invalid build override: remappings must be a list of strings")
    if not override["evidence"]:
        raise ValueError("invalid build override: evidence is required")
    warnings = override.get("warnings", [])
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ValueError("invalid build override: warnings must be a list of strings")
    result = {
        "settings": _json_copy(override["settings"]),
        "compiler_version": override["compiler_version"],
        "remappings": list(override["remappings"]),
        "origin": str(override.get("origin", "override")),
        "evidence": _json_copy(override["evidence"]),
        "warnings": list(warnings),
    }
    if "dependencies" in override:
        result["dependencies"] = _json_copy(override["dependencies"])
    return result


def discover_build(project: Path, override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return verified compiler settings and provenance for ``project``.

    Verified Hardhat/Foundry build-info takes precedence over declarative
    Foundry settings.  Compatible build-info files are coalesced as evidence;
    incompatible compiler/settings/remapping fingerprints raise an explicit
    ambiguity error.  JavaScript and TypeScript configuration is never
    executed or evaluated.
    """

    project = Path(project).resolve()
    if not project.is_dir():
        raise NotADirectoryError(project)
    if override is not None:
        if not isinstance(override, dict):
            raise ValueError("invalid build override: expected an object")
        return _override_result(override)

    warnings: list[str] = []
    candidates: list[dict[str, Any]] = []
    for path, origin in _build_info_paths(project):
        candidate, warning = _build_info_candidate(project, path.resolve(), origin)
        if candidate is None:
            if warning:
                warnings.append(warning)
        else:
            candidates.append(candidate)
    if candidates:
        groups: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            key = _settings_fingerprint(candidate["settings"], candidate["compiler_version"], candidate["remappings"])
            groups.setdefault(key, []).append(candidate)
        if len(groups) > 1:
            details = [
                f"{item['origin']}:{item['evidence'][0]['path']} compiler={item['compiler_version']}"
                for item in candidates
            ]
            raise ValueError("AMBIGUOUS_BUILD_CONFIGURATION: incompatible verified build-info settings: " + "; ".join(details))
        chosen = copy.deepcopy(candidates[0])
        chosen["evidence"] = [evidence for candidate in candidates for evidence in candidate["evidence"]]
        chosen["warnings"] = warnings
        return chosen

    foundry_path = project / "foundry.toml"
    if foundry_path.is_file():
        foundry, foundry_warnings = _foundry_settings(project, foundry_path)
        warnings.extend(foundry_warnings)
        if foundry is not None:
            remappings_path = project / "remappings.txt"
            if not foundry["remappings"] and remappings_path.is_file():
                remappings, remapping_warnings = _read_remappings(remappings_path)
                foundry["remappings"] = remappings
                if remappings:
                    foundry["settings"]["remappings"] = list(remappings)
                foundry["evidence"].append({"kind": "remappings.txt", "path": str(remappings_path.resolve()), "sha256": _sha256(remappings_path)})
                warnings.extend(remapping_warnings)
            elif foundry["remappings"]:
                foundry["settings"]["remappings"] = list(foundry["remappings"])
            foundry["warnings"] = warnings
            return foundry

    remappings_path = project / "remappings.txt"
    remappings: list[str] = []
    evidence: list[dict[str, Any]] = []
    if remappings_path.is_file():
        remappings, remapping_warnings = _read_remappings(remappings_path)
        warnings.extend(remapping_warnings)
        evidence.append({"kind": "remappings.txt", "path": str(remappings_path.resolve()), "sha256": _sha256(remappings_path)})
    dynamic = [name for name in _DYNAMIC_CONFIGS if (project / name).is_file()]
    if dynamic:
        warnings.append("dynamic build configuration was not executed: " + ", ".join(dynamic))
    elif not warnings:
        warnings.append("no verified build settings found; using source-closure defaults")
    return _fallback(project, warnings, remappings, evidence)


__all__ = ["discover_build"]
