"""Pinned, byte-preserving compilation plans and persisted compiler outputs."""
from __future__ import annotations

import hashlib
import json
import posixpath
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from crytic_compile import CryticCompile
from crytic_compile.compilation_unit import CompilationUnit
from crytic_compile.compiler.compiler import CompilerVersion
from crytic_compile.platform.solc_standard_json import SolcStandardJson, parse_standard_json_output

from .solc import compiler_command, compiler_fingerprint

SCHEMA = "spider-compilation-plan/1"
_SLITHER_RECURSION_LIMIT = 3000
_LEGACY_COMBINED_JSON_FIELDS = "abi,ast,bin,bin-runtime,srcmap,srcmap-runtime"


@contextmanager
def _slither_recursion_budget():
    """Give Slither enough stack for deeply nested generated Solidity expressions."""
    previous = sys.getrecursionlimit()
    raised = previous < _SLITHER_RECURSION_LIMIT
    if raised:
        sys.setrecursionlimit(_SLITHER_RECURSION_LIMIT)
    try:
        yield
    finally:
        if raised:
            sys.setrecursionlimit(previous)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _normalize_source_asts(output: dict[str, Any]) -> None:
    """Expose legacy solc ASTs through the standard-json AST field for parsers."""
    source_names = set(output.get("sources", {}))
    for source_name, source in output.get("sources", {}).items():
        if not source.get("ast") and source.get("legacyAST"):
            ast = source["legacyAST"]
            source["ast"] = ast
            pending = [ast]
            while pending:
                item = pending.pop()
                if isinstance(item, dict):
                    if item.get("name") == "ImportDirective":
                        attributes = item.get("attributes")
                        raw_path = attributes.get("file") if isinstance(attributes, dict) else None
                        if isinstance(raw_path, str) and isinstance(attributes, dict) and not attributes.get("absolutePath"):
                            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_name), raw_path.replace("\\", "/")))
                            if resolved in source_names:
                                attributes["absolutePath"] = resolved
                    pending.extend(item.values())
                elif isinstance(item, list):
                    pending.extend(item)


def _legacy_combined_json(version: str) -> bool:
    try:
        release = tuple(int(part) for part in version.split(".")[:3])
    except (TypeError, ValueError):
        return False
    return release < (0, 4, 11)


def _normalize_legacy_combined_output(output: dict[str, Any]) -> None:
    """Convert solc <0.4.11 combined-json output to the standard-json shape."""
    source_names = set(output.get("sources", {}))
    source_list = output.get("sourceList", [])
    source_ids = {name: index + 1 for index, name in enumerate(source_list)}
    normalized_sources: dict[str, dict[str, Any]] = {}

    for source_name, record in output.get("sources", {}).items():
        ast = record.get("AST")
        if not isinstance(ast, dict):
            raise ValueError(f"legacy solc output missing AST for {source_name}")
        pending: list[Any] = [ast]
        source_id: int | None = None
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                raw_source = item.get("src")
                if source_id is None and isinstance(raw_source, str):
                    try:
                        candidate = int(raw_source.rsplit(":", 1)[-1])
                    except ValueError:
                        candidate = -1
                    if candidate >= 0:
                        source_id = candidate
                if item.get("name") == "ImportDirective":
                    attributes = item.get("attributes")
                    raw_path = attributes.get("file") if isinstance(attributes, dict) else None
                    if isinstance(raw_path, str) and isinstance(attributes, dict) and not attributes.get("absolutePath"):
                        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_name), raw_path.replace("\\", "/")))
                        if resolved in source_names:
                            attributes["absolutePath"] = resolved
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        if "src" not in ast:
            source_id = source_id if source_id is not None else source_ids.get(source_name)
            if source_id is not None:
                ast["src"] = f"0:0:{source_id}"
        normalized_sources[source_name] = {"ast": ast}

    normalized_contracts: dict[str, dict[str, Any]] = {}
    for qualified_name, record in output.get("contracts", {}).items():
        if ":" not in qualified_name:
            raise ValueError(f"legacy solc output has invalid contract key: {qualified_name}")
        source_name, contract_name = qualified_name.rsplit(":", 1)
        abi = record.get("abi", "[]")
        if isinstance(abi, str):
            abi = json.loads(abi)
        normalized_contracts.setdefault(source_name, {})[contract_name] = {
            "abi": abi,
            "evm": {
                "bytecode": {
                    "object": record.get("bin", ""),
                    "sourceMap": record.get("srcmap", ""),
                },
                "deployedBytecode": {
                    "object": record.get("bin-runtime", ""),
                    "sourceMap": record.get("srcmap-runtime", ""),
                },
            },
        }

    output["sources"] = normalized_sources
    output["contracts"] = normalized_contracts


def _via_ir_supported(version: str) -> bool:
    try:
        major, minor, patch = (int(part) for part in version.split(".")[:3])
    except (TypeError, ValueError):
        return False
    return (major, minor, patch) >= (0, 8, 13)


def _compile_candidates(
    plan: dict[str, Any], standard: dict[str, Any], root: Path, artifacts: Path
) -> tuple[str, dict[str, Any], bool, list[dict[str, Any]], dict[str, Any]]:
    """Try pinned settings, then explicit optimizer/via-IR recoveries when safe."""
    candidates = plan.get("compiler_candidates") or [plan["compiler"]["requested"]]
    attempts: list[dict[str, Any]] = []
    last_output: dict[str, Any] | None = None

    def run(version: str, input_standard: dict[str, Any], recovery: str | None = None) -> tuple[str, dict[str, Any], bool] | None:
        nonlocal last_output
        fingerprint = compiler_fingerprint(version)
        suffix = f"-{recovery.lower()}" if recovery else ""
        attempt: dict[str, Any] = {
            "version": version,
            "fingerprint": fingerprint,
            "cache_hit": False,
            "settings": input_standard["settings"],
        }
        if recovery:
            attempt["recovery"] = recovery
        if not fingerprint.get("usable"):
            attempt.update(success=False, error="compiler fingerprint is not usable")
            attempts.append(attempt)
            return None
        cache_key = digest({"input": input_standard, "compiler": fingerprint})
        cache_path = artifacts / f"compiler-cache-{version}{suffix}.json"
        output_path = artifacts / f"output-{version}{suffix}.json"
        canonical_output = artifacts / "output.json"
        cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        cache_hit = (
            cached.get("key") == cache_key
            and output_path.exists()
            and canonical_output.exists()
            and cached.get("output_sha256") == hashlib.sha256(output_path.read_bytes()).hexdigest()
            and cached.get("output_sha256") == hashlib.sha256(canonical_output.read_bytes()).hexdigest()
        )
        try:
            if cache_hit:
                output = json.loads(output_path.read_text(encoding="utf-8"))
                returncode = cached["returncode"]
            else:
                legacy = _legacy_combined_json(version)
                command = (
                    compiler_command(version, "--combined-json", _LEGACY_COMBINED_JSON_FIELDS, *input_standard["sources"])
                    if legacy
                    else compiler_command(version, "--standard-json")
                )
                result = subprocess.run(
                    command,
                    input=None if legacy else json.dumps(input_standard).encode(),
                    capture_output=True,
                    cwd=root,
                )
                stdout_path = artifacts / f"solc-{version}{suffix}.stdout"
                stderr_path = artifacts / f"solc-{version}{suffix}.stderr"
                stdout_path.write_bytes(result.stdout)
                stderr_path.write_bytes(result.stderr)
                output = json.loads(result.stdout)
                if legacy:
                    _normalize_legacy_combined_output(output)
                returncode = result.returncode
                write_json(output_path, output)
                write_json(cache_path, {"key": cache_key, "returncode": returncode, "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest()})
            last_output = output
            write_json(artifacts / "output.json", output)
            errors = [item for item in output.get("errors", []) if item.get("severity") == "error"]
            attempt.update(cache_hit=cache_hit, returncode=returncode, error_count=len(errors), success=not returncode and not errors)
            if errors:
                attempt["diagnostics"] = [item.get("formattedMessage", item.get("message", str(item))) for item in errors[:8]]
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            attempt.update(success=False, error=f"{type(error).__name__}: {error}")
        attempts.append(attempt)
        if attempt.get("success"):
            write_json(
                artifacts / "compiler-cache.json",
                {
                    "key": cache_key,
                    "returncode": returncode,
                    "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
                    "selected": version,
                    "settings": input_standard["settings"],
                    "recovery": recovery,
                },
            )
            return version, output, cache_hit

        return None

    for version in candidates:
        selected = run(version, standard)
        if selected is not None:
            write_json(artifacts / "compiler-attempts.json", attempts)
            return (*selected, attempts, standard)

    stack_error = any("Stack too deep" in diagnostic for attempt in attempts for diagnostic in attempt.get("diagnostics", []))
    pinned_settings = plan.get("settings", {})
    if stack_error and "optimizer" not in pinned_settings and "viaIR" not in pinned_settings:
        recovery_standard = deepcopy(standard)
        recovery_standard["settings"]["optimizer"] = {"enabled": True, "runs": 200}
        write_json(artifacts / "input-recovery-optimizer.json", recovery_standard)
        for version in candidates:
            selected = run(version, recovery_standard, "optimizer")
            if selected is not None:
                write_json(artifacts / "compiler-attempts.json", attempts)
                return (*selected, attempts, recovery_standard)

        recovery_standard = deepcopy(standard)
        recovery_standard["settings"]["optimizer"] = {"enabled": True, "runs": 200}
        recovery_standard["settings"]["viaIR"] = True
        write_json(artifacts / "input-recovery-viair.json", recovery_standard)
        for version in candidates:
            if not _via_ir_supported(version):
                continue
            selected = run(version, recovery_standard, "viaIR")
            if selected is not None:
                write_json(artifacts / "compiler-attempts.json", attempts)
                return (*selected, attempts, recovery_standard)

    write_json(artifacts / "compiler-attempts.json", attempts)
    if last_output is not None:
        write_json(artifacts / "output.json", last_output)
    diagnostics = [item.get("diagnostics", [item.get("error", "unknown compiler failure")])[0] for item in attempts]
    raise ValueError("all compiler candidates failed: " + " | ".join(diagnostics))


def load_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("schema") != SCHEMA or not isinstance(plan.get("sources"), dict) or not plan["sources"]:
        raise ValueError("invalid compilation plan schema/sources")
    if not isinstance(plan.get("project_id"), str) or not plan["project_id"]:
        raise ValueError("missing project_id")
    if plan.get("unit_id") != digest({k: v for k, v in plan.items() if k != "unit_id"}):
        raise ValueError("compilation unit digest mismatch")
    for name, record in plan["sources"].items():
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name or str(p) != name:
            raise ValueError(f"unsafe source-unit name: {name}")
        source = Path(record["path"])
        if not source.is_absolute() or hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError(f"source hash/path mismatch: {name}")
        source.read_bytes().decode("utf-8", errors="strict")
    if not plan.get("entries") or not set(plan["entries"]) <= plan["sources"].keys():
        raise ValueError("invalid plan entries")
    version = plan["compiler"]["requested"]
    if plan["compiler"] != compiler_fingerprint(version) or not plan["compiler"]["usable"]:
        raise ValueError("compiler fingerprint mismatch")
    if not isinstance(plan.get("settings"), dict):
        raise ValueError("invalid settings")
    return plan


class _CompiledJson(SolcStandardJson):
    """Use persisted solc output without a second compiler invocation."""

    def __init__(self, standard: dict, output: dict, version: str):
        super().__init__(standard)
        self.output = output
        self.version = version

    def compile(self, crytic_compile: CryticCompile, **kwargs: Any) -> None:
        unit = CompilationUnit(crytic_compile, "standard_json")
        optimizer = self.to_dict()["settings"].get("optimizer", {})
        unit.compiler_version = CompilerVersion("solc", self.version, optimizer.get("enabled", False), optimizer.get("runs"))
        parse_standard_json_output(self.output, unit, solc_working_dir=kwargs["solc_working_dir"])


def extract_plan(plan_path: Path, artifacts: Path) -> dict[str, Any]:
    # Import the facade so existing Slither compatibility fixes apply here too.
    from slither.slither import Slither

    from .extract import _build_graph
    from .verify import validate

    stage = "plan"
    status: dict[str, Any] = {"solc_ok": False, "slither_ok": False, "graph_built": False, "graph_valid": False}
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        plan = load_plan(plan_path)
        root = (artifacts / "sources").resolve()
        source_bytes = {}
        for name, record in plan["sources"].items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = Path(record["path"]).read_bytes()
            if target.exists() and target.read_bytes() != raw:
                raise ValueError(f"stale staged source: {name}")
            target.write_bytes(raw)
            source_bytes[str(target)] = raw
        standard = {"language": "Solidity", "sources": {n: {"content": source_bytes[str(root / n)].decode("utf-8")} for n in plan["sources"]}, "settings": plan["settings"].copy()}
        standard["settings"]["outputSelection"] = {"*": {"*": ["abi", "evm.bytecode", "evm.deployedBytecode", "devdoc", "userdoc"], "": ["ast"]}}
        write_json(artifacts / "input.json", standard)
        stage = "solc"
        version, output, cache_hit, compiler_attempts, selected_standard = _compile_candidates(plan, standard, root, artifacts)
        status["compiler_cache_hit"] = cache_hit
        status["compiler_attempts"] = compiler_attempts
        status["selected_compiler"] = compiler_fingerprint(version)
        status["selected_settings"] = selected_standard["settings"]
        errors = [e for e in output.get("errors", []) if e.get("severity") == "error"]
        if errors:
            raise ValueError("\n".join(e.get("formattedMessage", e.get("message", str(e))) for e in errors))
        _normalize_source_asts(output)
        if set(output.get("sources", {})) != set(selected_standard["sources"]) or any(not s.get("ast") for s in output["sources"].values()):
            raise ValueError("compiler source/AST coverage mismatch")
        status["solc_ok"] = True
        stage = "slither"
        with _slither_recursion_budget():
            compilation = CryticCompile(_CompiledJson(selected_standard, output, version), solc_working_dir=str(root))
            slither = Slither(compilation)
            status["slither_ok"] = True
            stage = "graph"
            graph = _build_graph(slither, root, source_bytes, [root / n for n in sorted(plan["sources"])], version, "", True)
        stable_attempts = [{key: value for key, value in attempt.items() if key != "cache_hit"} for attempt in compiler_attempts]
        graph["graph"].update(
            compilation_unit_id=plan["unit_id"],
            compilation_plan_digest=digest(plan),
            compilation_plan=plan,
            compiler_selection={"selected": compiler_fingerprint(version), "attempts": stable_attempts, "selected_settings": selected_standard["settings"]},
        )
        status["graph_built"] = True
        stage = "verifier"
        errors = validate(graph)
        contracts = {(str(root / filename).replace("\\", "/"), name) for filename, items in output.get("contracts", {}).items() for name in items}
        represented = {(n.get("file"), n.get("contract_name")) for n in graph["nodes"] if n.get("declaration_role") == "contract"}
        if contracts - represented:
            errors.append(f"compiler contract coverage mismatch: {sorted(contracts - represented)}")
        write_json(artifacts / "verification.json", {"errors": errors, "compiler_contracts": len(contracts)})
        if errors:
            raise ValueError("\n".join(errors))
        status["graph_valid"] = True
        return graph
    except BaseException as error:
        status.update(stage=stage, error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(artifacts / "status.json", status)
