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
from spider.resolver import resolve_closure
from spider.solc import compatible_project_versions, compiler_fingerprint, installed_solc_versions, pragma_expressions


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


def build(inventory_path: Path, output: Path) -> dict:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    root = Path(inventory["source_root"])
    versions = installed_solc_versions()
    source_records = [record for record in inventory["files"] if record.get("kind", "solidity") == "solidity"]
    units, blocked, seen = [], [], {}
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
            stage = "dependency"
            closure = resolve_closure(project, [entry], remappings)
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
            settings["remappings"] = remappings
            closure_key = digest({"project": project_id, "sources": sources, "settings": settings, "compiler": candidates[0]})
            if closure_key in seen:
                seen[closure_key]["files"].append(filename)
                continue
            plan = {"schema": SCHEMA, "project_id": project_id, "sources": sources, "entries": sorted(sources),
                    "compiler": compiler_fingerprint(candidates[0]), "settings": settings, "dependencies": [],
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
    args = parser.parse_args()
    print(json.dumps(build(args.inventory, args.output), indent=2))
