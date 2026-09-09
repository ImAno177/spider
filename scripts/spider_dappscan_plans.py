"""Create pinned per-closure Spider plans without changing DAppSCAN sources."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spider.build_environment import discover_build
from spider.compilation import SCHEMA, digest, write_json
from spider.resolver import imports, resolve_closure
from spider.solc import compatible_project_versions, compiler_fingerprint, installed_solc_versions, pragma_expressions


def _remapping_prefix(value: str) -> str:
    """Return the prefix portion of a Solidity remapping."""

    left = value.split("=", 1)[0]
    return left.rsplit(":", 1)[-1].rstrip("/") + "/"


def _observed_package_imports(project: Path) -> dict[str, set[str]]:
    observed: dict[str, set[str]] = defaultdict(set)
    for source in sorted(project.rglob("*.sol")):
        try:
            imports_in_source = imports(source.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError):
            continue
        for imported in imports_in_source:
            imported = imported.replace("\\", "/")
            if imported.startswith(("./", "../", "/")):
                continue
            parts = imported.split("/")
            if imported.startswith("@"):
                if len(parts) < 3:
                    continue
                prefix = "/".join(parts[:2]) + "/"
                suffix = "/".join(parts[2:])
            else:
                if len(parts) < 2:
                    continue
                prefix = parts[0] + "/"
                suffix = "/".join(parts[1:])
            observed[prefix].add(suffix)
    return observed


def infer_local_package_remappings(
    project: Path,
    existing: list[str],
    observed: dict[str, set[str]] | None = None,
) -> tuple[list[str], list[dict], list[str]]:
    """Infer only verifiable monorepo package aliases already in ``project``.

    A number of DAppSCAN projects preserve a monorepo's package directories but
    omit its generated ``remappings.txt``.  The 0x layout, for example, keeps
    ``contracts/utils`` while sources import ``@0x/contracts-utils``.  We add a
    remapping only when every observed suffix for the package exists below one
    unique project-local target; no corpus-wide or basename search is used.
    """

    observed = observed or _observed_package_imports(project)
    existing_prefixes = {_remapping_prefix(item) for item in existing}
    inferred: list[str] = []
    evidence: list[dict] = []
    warnings: list[str] = []
    for prefix, suffixes in sorted(observed.items()):
        if prefix in existing_prefixes:
            continue
        package = prefix.rstrip("/").split("/")[-1]
        # This convention is the package-to-directory relationship used by
        # the preserved 0x-style monorepos. Other package names need an
        # explicit build remapping or dependency provenance.
        if not package.startswith("contracts-"):
            continue
        package_dir = package.removeprefix("contracts-")
        candidates = []
        for target in (project / "contracts" / package_dir, project / "packages" / package_dir, project / package_dir):
            if target.is_dir() and all((target / suffix).is_file() for suffix in suffixes):
                candidates.append(target.resolve())
        if len(candidates) > 1:
            warnings.append(
                f"local package remapping ambiguous for {prefix}: "
                + ", ".join(str(item) for item in candidates)
            )
            continue
        if not candidates:
            continue
        target = candidates[0]
        relative_target = target.relative_to(project).as_posix().rstrip("/") + "/"
        inferred.append(f"{prefix}={relative_target}")
        evidence.append(
            {
                "kind": "dappscan-local-package-layout",
                "prefix": prefix,
                "target": relative_target,
                "observed_suffixes": sorted(suffixes),
            }
        )
    return inferred, evidence, warnings


def _load_dependency_lock(path: Path | None) -> list[dict]:
    if path is None:
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "spider-dappscan-dependency-lock/1" or not isinstance(value.get("entries"), list):
        raise ValueError("invalid DAppSCAN dependency lock schema")
    entries: list[dict] = []
    seen_prefixes: set[str] = set()
    scoped_prefixes: dict[str, set[str]] = defaultdict(set)
    for index, item in enumerate(value["entries"]):
        if not isinstance(item, dict):
            raise ValueError(f"dependency lock entry {index} must be an object")
        prefix = item.get("prefix")
        root_value = item.get("root")
        if not isinstance(prefix, str) or not prefix or not prefix.endswith("/"):
            raise ValueError(f"dependency lock entry {index} has invalid prefix")
        projects = item.get("projects")
        if projects is not None:
            if (
                not isinstance(projects, list)
                or not projects
                or any(not isinstance(project, str) or not project for project in projects)
                or len(set(projects)) != len(projects)
            ):
                raise ValueError(f"dependency lock entry {index} has invalid projects")
            overlap = scoped_prefixes[prefix].intersection(projects)
            if overlap:
                raise ValueError(
                    f"duplicate dependency lock prefix/project: {prefix} / {sorted(overlap)}"
                )
            scoped_prefixes[prefix].update(projects)
        elif prefix in seen_prefixes or scoped_prefixes.get(prefix):
            raise ValueError(f"duplicate dependency lock prefix: {prefix}")
        if not isinstance(root_value, str) or not root_value:
            raise ValueError(f"dependency lock entry {index} has invalid root")
        root = Path(root_value).resolve()
        if not root.is_dir():
            raise ValueError(f"dependency lock root does not exist: {root}")
        archive_value = item.get("archive")
        archive_sha256 = item.get("archive_sha256")
        if archive_value is not None:
            if not isinstance(archive_value, str) or not archive_value:
                raise ValueError(f"dependency lock entry {index} has invalid archive")
            if not isinstance(archive_sha256, str) or len(archive_sha256) != 64:
                raise ValueError(f"dependency lock entry {index} has invalid archive_sha256")
            archive = Path(archive_value).resolve()
            if not archive.is_file():
                raise ValueError(f"dependency lock archive does not exist: {archive}")
            digest = hashlib.sha256()
            with archive.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != archive_sha256.lower():
                raise ValueError(f"dependency lock archive checksum mismatch: {archive}")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(f"dependency lock entry {index} has invalid id")
        safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in identifier).strip("-")
        if not safe:
            raise ValueError(f"dependency lock entry {index} has no safe virtual name")
        entry = dict(item)
        entry["root"] = str(root)
        entry["virtual_prefix"] = f"dependencies/{safe}/"
        entries.append(entry)
        seen_prefixes.add(prefix)
    return entries


def _locked_entry_for_project(entry: dict, project_id: str) -> bool:
    projects = entry.get("projects")
    return projects is None or project_id in projects


def infer_locked_package_remappings(
    existing: list[str],
    observed: dict[str, set[str]],
    lock_entries: list[dict],
    project_id: str = "",
) -> tuple[list[str], list[str], list[dict], list[dict], list[str]]:
    """Return physical/virtual remappings and provenance for locked packages."""

    existing_prefixes = {_remapping_prefix(item) for item in existing}
    resolution: list[str] = []
    compiler: list[str] = []
    evidence: list[dict] = []
    selected: list[dict] = []
    warnings: list[str] = []
    entries_by_prefix: dict[str, list[dict]] = defaultdict(list)
    for entry in lock_entries:
        if _locked_entry_for_project(entry, project_id):
            entries_by_prefix[entry["prefix"]].append(entry)
    for prefix, candidates in sorted(entries_by_prefix.items()):
        if prefix in existing_prefixes or prefix not in observed:
            continue
        if len(candidates) > 1:
            warnings.append(
                f"AMBIGUOUS_LOCKED_DEPENDENCY: project={project_id!r} prefix={prefix!r} "
                f"candidates={[entry['id'] for entry in candidates]}"
            )
            continue
        entry = candidates[0]
        root = Path(entry["root"])
        suffixes = observed[prefix]
        if not all((root / suffix).is_file() for suffix in suffixes):
            warnings.append(f"locked dependency is missing an observed source: {prefix}")
            continue
        resolution.append(f"{prefix}={root.as_posix().rstrip('/')}/")
        compiler.append(f"{prefix}={entry['virtual_prefix']}")
        selected_entry = dict(entry)
        selected.append(selected_entry)
        evidence.append(
            {
                "kind": "dappscan-locked-dependency",
                "id": entry["id"],
                "prefix": prefix,
                "root": str(root),
                "archive_sha256": entry.get("archive_sha256"),
                "integrity": entry.get("integrity"),
            }
        )
    return resolution, compiler, evidence, selected, warnings


def _virtualize_locked_closure(closure: dict[str, Path], lock_entries: list[dict]) -> dict[str, Path]:
    rewritten: dict[str, Path] = {}
    for logical, physical in closure.items():
        virtual_name = logical
        matches = []
        for entry in lock_entries:
            root = Path(entry["root"]).resolve()
            try:
                relative = physical.resolve().relative_to(root)
            except ValueError:
                continue
            matches.append(f"{entry['virtual_prefix']}{relative.as_posix()}")
        if len(matches) > 1:
            raise ValueError(f"AMBIGUOUS_DEPENDENCY: physical source belongs to multiple locked roots: {physical}")
        if matches:
            virtual_name = matches[0]
        previous = rewritten.get(virtual_name)
        if previous is not None and previous != physical:
            raise ValueError(f"AMBIGUOUS_DEPENDENCY: virtual source name has multiple files: {virtual_name}")
        rewritten[virtual_name] = physical
    return dict(sorted(rewritten.items()))


def group_units(units: list[dict], output: Path) -> list[dict]:
    """Keep a project group only when its combined code generation succeeds."""
    from solc_select.solc_select import artifact_path

    groups = defaultdict(list)
    for unit in units:
        plan = json.loads(Path(unit["plan"]).read_text())
        groups[(unit["project_id"], digest(plan["settings"]), plan["compiler"]["requested"])].append((unit, plan))
    result = []
    for members in groups.values():
        if len(members) == 1:
            result.append(members[0][0])
            continue
        plan = dict(members[0][1])
        plan.pop("unit_id")
        merged = {}
        conflict = False
        for _, member in members:
            for name, source in member["sources"].items():
                if name in merged and merged[name] != source:
                    conflict = True
                merged[name] = source
        if conflict:
            result.extend(u for u, _ in members)
            continue
        plan["sources"] = dict(sorted(merged.items()))
        plan["entries"] = sorted(merged)
        plan["unit_id"] = digest(plan)
        settings = dict(plan["settings"], outputSelection={"*": {"*": ["abi", "evm.bytecode", "evm.deployedBytecode"], "": ["ast"]}})
        standard = {"language": "Solidity", "sources": {n: {"content": Path(r["path"]).read_bytes().decode("utf-8")} for n, r in merged.items()}, "settings": settings}
        probe_path = output / "group-probes" / (plan["unit_id"] + ".json")
        try:
            completed = subprocess.run([str(artifact_path(plan["compiler"]["requested"])), "--standard-json"], input=json.dumps(standard).encode(), capture_output=True, timeout=600)
            compiled = json.loads(completed.stdout)
            errors = [e for e in compiled.get("errors", []) if e.get("severity") == "error"]
            success = not completed.returncode and not errors and set(compiled.get("sources", {})) == set(merged)
            write_json(probe_path, {"success": success, "errors": errors, "returncode": completed.returncode})
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            success = False
            write_json(probe_path, {"success": False, "error": str(error)})
        if success:
            path = (output / "plans" / (plan["unit_id"] + ".json")).resolve()
            write_json(path, plan)
            result.append({"unit_id": plan["unit_id"], "project_id": plan["project_id"], "plan": str(path), "files": sorted({f for u, _ in members for f in u["files"]})})
        else:
            result.extend(u for u, _ in members)
    return result


def build(inventory_path: Path, output: Path, dependency_lock: Path | None = None) -> dict:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    root = Path(inventory["source_root"])
    lock_entries = _load_dependency_lock(dependency_lock)
    versions = installed_solc_versions()
    source_records = [record for record in inventory["files"] if record.get("kind", "solidity") == "solidity"]
    units, blocked, seen = [], [], {}
    inferred_by_project: dict[Path, tuple[list[str], list[dict], list[str], list[str], list[str], list[dict], list[dict], list[str]]] = {}
    output.mkdir(parents=True, exist_ok=True)
    for record in source_records:
        filename, project_id = record["path"], record["project_id"]
        stage = "discovery"
        try:
            source = root / filename
            if hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError("inventory source checksum mismatch")
            project = root / project_id
            entry = source.relative_to(project).as_posix()
            build = discover_build(project)
            remappings = list(build["remappings"])
            cached_inference = inferred_by_project.get(project)
            if cached_inference is None:
                observed = _observed_package_imports(project)
                local, local_evidence, local_warnings = infer_local_package_remappings(project, remappings, observed)
                locked_resolution, locked_compiler, locked_evidence, selected_dependencies, locked_warnings = infer_locked_package_remappings(
                    remappings + local, observed, lock_entries, project_id
                )
                cached_inference = (local, local_evidence, local_warnings, locked_resolution, locked_compiler, locked_evidence, selected_dependencies, locked_warnings)
                inferred_by_project[project] = cached_inference
            inferred, inference_evidence, inference_warnings, locked_resolution, locked_compiler, locked_evidence, selected_dependencies, locked_warnings = cached_inference
            remappings.extend(inferred)
            resolution_remappings = [*remappings, *locked_resolution]
            compiler_remappings = [*remappings, *locked_compiler]
            build["evidence"] = [*build["evidence"], *inference_evidence, *locked_evidence]
            build["warnings"] = [*build["warnings"], *inference_warnings, *locked_warnings]
            stage = "dependency"
            closure = _virtualize_locked_closure(resolve_closure(project, [entry], resolution_remappings), selected_dependencies)
            stage = "compiler_selection"
            expressions = [expression for p in closure.values() for expression in pragma_expressions(p)]
            pinned = build.get("compiler_version")
            candidates = [pinned] if pinned else compatible_project_versions(expressions, versions)
            if not candidates:
                raise ValueError(f"NO_COMPATIBLE_INSTALLED_COMPILER: {sorted(set(expressions))}")
            # Unpinned projects often use a broad lower-bound pragma while
            # relying on syntax introduced by a later compiler.  Try the
            # newest compatible release first; every candidate remains in the
            # plan for deterministic repair/replay if that attempt fails.
            if not pinned:
                candidates = sorted(candidates, key=lambda value: tuple(map(int, value.split("."))), reverse=True)
            sources = {n: {"path": str(p.resolve()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for n, p in sorted(closure.items())}
            settings = dict(build["settings"])
            settings["remappings"] = compiler_remappings
            closure_key = digest({"project": project_id, "sources": sources, "settings": settings, "compiler": candidates[0], "dependencies": selected_dependencies})
            if closure_key in seen:
                seen[closure_key]["files"].append(filename)
                continue
            plan = {"schema": SCHEMA, "project_id": project_id, "sources": sources, "entries": sorted(sources),
                    "compiler": compiler_fingerprint(candidates[0]), "settings": settings, "dependencies": selected_dependencies,
                    "environment_origin": build["origin"], "build_evidence": build["evidence"],
                    "build_warnings": build["warnings"], "compiler_candidates": candidates}
            plan["unit_id"] = digest(plan)
            plan_path = (output / "plans" / (plan["unit_id"] + ".json")).resolve()
            write_json(plan_path, plan)
            unit = {"unit_id": plan["unit_id"], "project_id": project_id, "plan": str(plan_path), "files": [filename]}
            seen[closure_key] = unit
            units.append(unit)
        except (ValueError, OSError, UnicodeError) as error:
            blocked.append({"project_id": project_id, "files": [filename], "stage": stage, "error": f"{type(error).__name__}: {error}"})
    units = group_units(units, output)
    manifest = {"schema": "spider-project-manifest/1", "snapshot_commit": inventory.get("commit", inventory.get("source_commit")),
                "inventory_sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(), "inventory_files": source_records,
                "all_inventory_file_count": len(inventory["files"]), "units": units, "blocked": blocked}
    write_json(output / "manifest.json", manifest)
    return {"files": len(source_records), "units": len(units), "blocked": len(blocked)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--dependency-lock", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.inventory, args.output, args.dependency_lock), indent=2))
