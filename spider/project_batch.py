"""Run checksum-pinned Spider compilation units as a resumable corpus batch.

The runner deliberately keeps the unit process boundary small: one ``spider``
process owns compiler/Slither state, while this module only schedules units,
persists checkpoints, and validates completed artifacts before reusing them.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .compilation import digest, load_plan, write_json

PROJECT_MANIFEST_SCHEMA = "spider-project-manifest/1"
CHECKPOINT_SCHEMA = "spider-project-batch-checkpoint/1"
_UNIT_ID = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_WORKERS = 32
_RETRY_TIMEOUT = 1800.0
_STAGE_KEYS = ("solc_ok", "slither_ok", "graph_built", "graph_valid")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value or str(path) != value:
        raise ValueError(f"unsafe {label}: {value}")
    return value


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != PROJECT_MANIFEST_SCHEMA:
        raise ValueError(f"invalid project manifest schema: expected {PROJECT_MANIFEST_SCHEMA}")
    if not isinstance(value.get("units"), list):
        raise ValueError("project manifest units must be a list")
    if not isinstance(value.get("blocked"), list):
        raise ValueError("project manifest blocked must be a list")
    if not isinstance(value.get("inventory_files"), list):
        raise ValueError("project manifest inventory_files must be a list")
    if not isinstance(value.get("snapshot_commit"), str) or not value["snapshot_commit"]:
        raise ValueError("project manifest snapshot_commit is required")

    seen: set[str] = set()
    for index, unit in enumerate(value["units"]):
        if not isinstance(unit, dict):
            raise ValueError(f"unit {index} must be an object")
        unit_id = unit.get("unit_id")
        if not isinstance(unit_id, str) or not _UNIT_ID.fullmatch(unit_id):
            raise ValueError(f"unit {index} has unsafe unit_id")
        if unit_id in seen:
            raise ValueError(f"duplicate unit_id: {unit_id}")
        seen.add(unit_id)
        if not isinstance(unit.get("project_id"), str) or not unit["project_id"]:
            raise ValueError(f"unit {unit_id} is missing project_id")
        plan = unit.get("plan")
        if not isinstance(plan, str) or not Path(plan).is_absolute():
            raise ValueError(f"unit {unit_id} plan must be an absolute path")
        if not isinstance(unit.get("files"), list):
            raise ValueError(f"unit {unit_id} files must be a list")
        for file_name in unit["files"]:
            _safe_relative(file_name, f"unit {unit_id} file")

    for index, blocked in enumerate(value["blocked"]):
        if not isinstance(blocked, dict):
            raise ValueError(f"blocked entry {index} must be an object")
        if not isinstance(blocked.get("project_id"), str) or not blocked["project_id"]:
            raise ValueError(f"blocked entry {index} is missing project_id")
        if not isinstance(blocked.get("files"), list):
            raise ValueError(f"blocked entry {index} files must be a list")
        for file_name in blocked["files"]:
            _safe_relative(file_name, f"blocked entry {index} file")
        if not isinstance(blocked.get("error"), str) or not blocked["error"]:
            raise ValueError(f"blocked entry {index} is missing error")
        if not isinstance(blocked.get("stage"), str) or not blocked["stage"]:
            raise ValueError(f"blocked entry {index} is missing stage")

    for index, item in enumerate(value["inventory_files"]):
        if not isinstance(item, dict):
            raise ValueError(f"inventory file {index} must be an object")
        _safe_relative(item.get("path"), f"inventory file {index} path")
        if not isinstance(item.get("project_id"), str) or not item["project_id"]:
            raise ValueError(f"inventory file {index} is missing project_id")
        if not isinstance(item.get("sha256"), str) or not _SHA256.fullmatch(item["sha256"]):
            raise ValueError(f"inventory file {index} has invalid sha256")
    return value


def _package_version(distribution: str, module_name: str | None = None) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        if module_name:
            try:
                module = __import__(module_name)
                return str(getattr(module, "__version__"))
            except (ImportError, AttributeError):
                pass
    return None


def _runtime_signature() -> dict[str, Any]:
    package_root = Path(__file__).resolve().parent
    names = ("project_batch.py", "compilation.py", "extract.py", "_builder.py", "_graph.py", "verify.py", "__main__.py", "solc.py", "resolver.py", "build_environment.py")
    code_sha256 = {
        name: _sha256(package_root / name)
        for name in names
        if (package_root / name).is_file()
    }
    tools = {
        "spider": __version__,
        "slither": _package_version("slither-analyzer", "slither"),
        "solc_select": _package_version("solc-select", "solc_select"),
        "crytic_compile": _package_version("crytic-compile", "crytic_compile"),
    }
    payload = {"tools": tools, "code_sha256": code_sha256}
    return {**payload, "digest": digest(payload)}


def _kill_process_tree(process: subprocess.Popen[bytes], process_group: int | None = None) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(process_group if process_group is not None else os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _run_process(command: list[str], timeout: float, cwd: Path) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": str(cwd),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, **kwargs)
    except BaseException as error:
        return {
            "returncode": None,
            "stdout": b"",
            "stderr": str(error).encode("utf-8", errors="replace"),
            "timeout": False,
            "seconds": round(time.monotonic() - started, 3),
        }
    process_group = process.pid if os.name != "nt" else None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return {
            "returncode": process.returncode,
            "stdout": stdout or b"",
            "stderr": stderr or b"",
            "timeout": False,
            "seconds": round(time.monotonic() - started, 3),
        }

    except subprocess.TimeoutExpired:
        _kill_process_tree(process, process_group)
        stdout, stderr = process.communicate()
        return {
            "returncode": process.returncode,
            "stdout": stdout or b"",
            "stderr": stderr or b"",
            "timeout": True,
            "seconds": round(time.monotonic() - started, 3),
        }


def _verify_graph(graph_path: Path, timeout: float, cwd: Path) -> list[str]:
    """Run the independent verifier in its own bounded process."""
    process = _run_process([sys.executable, "-m", "spider.verify", str(graph_path)], timeout, cwd)
    if process["timeout"]:
        return [f"independent verifier timed out after {timeout:g}s"]
    if process["returncode"] == 0:
        return []
    stdout = process["stdout"].decode("utf-8", errors="replace")
    errors = [line[2:] for line in stdout.splitlines() if line.startswith("- ")]
    if errors:
        return errors
    detail = process["stderr"].decode("utf-8", errors="replace").strip() or stdout.strip()
    return [detail or f"independent verifier exited with code {process['returncode']}"]


def _artifact_checksums(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _clear_generated_artifacts(unit_root: Path) -> None:
    # Keep compiler evidence/cache for replay; remove only stale success markers.
    for path in (unit_root / "graph.json", unit_root / "graph.compilation" / "status.json"):
        if path.exists():
            if not path.resolve().is_relative_to(unit_root.resolve()):
                raise ValueError("generated artifact escapes unit directory")
            path.unlink()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _plan_digest_without_validation(plan_path: Path) -> str | None:
    try:
        value = _read_json(plan_path)
        return digest(value) if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _stage_state(compilation_root: Path) -> tuple[dict[str, bool], str | None, str | None]:
    status_path = compilation_root / "status.json"
    if not status_path.is_file():
        return {key: False for key in _STAGE_KEYS}, None, None
    try:
        status = _read_json(status_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return {key: False for key in _STAGE_KEYS}, "status", f"invalid status.json: {error}"
    if not isinstance(status, dict):
        return {key: False for key in _STAGE_KEYS}, "status", "status.json is not an object"
    return ({key: status.get(key) is True for key in _STAGE_KEYS}, status.get("stage"), status.get("error"))


def _result_base(unit: dict[str, Any], output: Path, runtime: dict[str, Any], attempt: int) -> dict[str, Any]:
    unit_root = output / "units" / unit["unit_id"]
    return {
        "unit_id": unit["unit_id"],
        "project_id": unit["project_id"],
        "files": list(unit["files"]),
        "plan": unit["plan"],
        "graph": (unit_root / "graph.json").relative_to(output).as_posix(),
        "status": "pending",
        "stage": "plan",
        "stages": {key: False for key in _STAGE_KEYS},
        "graph_sha256": None,
        "artifact_checksums": {},
        "returncode": None,
        "attempt": attempt,
        "cache_hit": False,
        "runtime_signature": runtime["digest"],
        "plan_digest": _plan_digest_without_validation(Path(unit["plan"])),
        "seconds": 0.0,
    }


def _runtime_compatible(previous: Any, current: dict[str, Any]) -> bool:
    """Allow a runner-only change to reuse already verified graph artifacts."""
    if not isinstance(previous, dict):
        return False
    if previous.get("tools") != current.get("tools"):
        return False
    before = previous.get("code_sha256")
    after = current.get("code_sha256")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    keys = set(before) | set(after)
    stable = keys - {"project_batch.py"}
    return bool(stable) and all(before.get(key) == after.get(key) for key in stable)


def _run_unit(unit: dict[str, Any], output: Path, runtime: dict[str, Any], timeout: float, attempt: int = 1) -> dict[str, Any]:
    unit_root = output / "units" / unit["unit_id"]
    unit_root.mkdir(parents=True, exist_ok=True)
    _clear_generated_artifacts(unit_root)
    graph_path = unit_root / "graph.json"
    command = [sys.executable, "-m", "spider", "--compilation-plan", unit["plan"], str(graph_path)]
    result = _result_base(unit, output, runtime, attempt)
    process = _run_process(command, timeout, Path(__file__).resolve().parents[1])
    stdout_name = "stdout.log" if attempt == 1 else f"stdout.retry-{attempt}.log"
    stderr_name = "stderr.log" if attempt == 1 else f"stderr.retry-{attempt}.log"
    (unit_root / stdout_name).write_bytes(process["stdout"])
    (unit_root / stderr_name).write_bytes(process["stderr"])
    result["returncode"] = process["returncode"]
    result["seconds"] = process["seconds"]
    result["timeout"] = process["timeout"]
    compilation_root = unit_root / "graph.compilation"
    stages, stage, stage_error = _stage_state(compilation_root)
    result["stages"] = stages
    result["stage"] = stage or ("timeout" if process["timeout"] else "process")
    if stage_error:
        result["error"] = stage_error
    if process["timeout"]:
        result["status"] = "timeout"
        result["error"] = f"subprocess timed out after {timeout:g}s"
    elif process["returncode"] != 0:
        result["status"] = "error"
        result["error"] = result.get("error") or process["stderr"].decode("utf-8", errors="replace").strip() or f"spider exited with code {process['returncode']}"
    elif not graph_path.is_file():
        result["status"] = "error"
        result["stage"] = result.get("stage") or "graph"
        result["error"] = result.get("error") or "graph output was not created"
    else:
        result["graph_sha256"] = _sha256(graph_path)
        try:
            graph = _read_json(graph_path)
            errors = _verify_graph(graph_path, timeout, Path(__file__).resolve().parents[1])
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            errors = [f"invalid graph JSON: {error}"]
        if errors:
            result["status"] = "error"
            result["stage"] = "verifier"
            result["error"] = "; ".join(errors)
        elif graph.get("graph", {}).get("compilation_unit_id") != unit["unit_id"]:
            result["status"] = "error"
            result["stage"] = "provenance"
            result["error"] = "graph compilation_unit_id does not match manifest unit_id"
        elif not all(stages.values()):
            result["status"] = "error"
            result["stage"] = stage or "compilation"
            result["error"] = result.get("error") or "compilation stage did not complete"
        else:
            result["status"] = "ok"
            result["stage"] = "complete"
    result["artifact_checksums"] = _artifact_checksums(unit_root)
    return result


def _validate_cached_result(
    unit: dict[str, Any],
    previous: dict[str, Any],
    output: Path,
    runtime: dict[str, Any],
    compatible_runtime: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(previous, dict) or previous.get("status") != "ok":
        return None
    if previous.get("runtime_signature") != runtime["digest"] and not compatible_runtime:
        return None
    plan_path = Path(unit["plan"])
    try:
        plan = load_plan(plan_path)
    except (BaseException,):
        return None
    if plan.get("unit_id") != unit["unit_id"] or plan.get("project_id") != unit["project_id"] or previous.get("plan_digest") != digest(plan):
        return None
    unit_root = output / "units" / unit["unit_id"]
    checksums = previous.get("artifact_checksums")
    if not isinstance(checksums, dict):
        return None
    if not previous.get("cache_recovered") and checksums != _artifact_checksums(unit_root):
        return None
    graph_path = unit_root / "graph.json"
    if not graph_path.is_file() or (
        not previous.get("cache_recovered") and previous.get("graph_sha256") != _sha256(graph_path)
    ):
        return None
    if previous.get("cache_recovered"):
        if not graph_path.is_file():
            return None
    else:
        try:
            graph = _read_json(graph_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if graph.get("graph", {}).get("compilation_unit_id") != unit["unit_id"]:
            return None
    # The artifact was accepted by the verifier before it entered the cache;
    # checksum and runtime compatibility checks are sufficient on resume.
    stages, _, _ = _stage_state(unit_root / "graph.compilation")
    if not all(stages.values()):
        return None
    reused = dict(previous)
    reused["files"] = list(unit["files"])
    reused["plan"] = unit["plan"]
    reused["cache_hit"] = True
    reused["attempt"] = 0
    reused["runtime_signature"] = runtime["digest"]
    reused.pop("cache_recovered", None)
    return reused


def _load_checkpoint(path: Path) -> dict[str, Any]:
    try:
        value = _read_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != CHECKPOINT_SCHEMA or not isinstance(value.get("results"), list):
        return {}
    return value


def _write_checkpoint(output: Path, manifest_path: Path, manifest_digest: str, manifest: dict[str, Any], runtime: dict[str, Any], results: dict[str, dict[str, Any]]) -> None:
    write_json(
        output / "checkpoint.json",
        {
            "schema": CHECKPOINT_SCHEMA,
            "manifest": str(manifest_path),
            "manifest_digest": manifest_digest,
            "snapshot_commit": manifest["snapshot_commit"],
            "runtime_signature": runtime,
            "results": [results[key] for key in sorted(results)],
            "updated_at": _now(),
        },
    )


def _coverage(manifest: dict[str, Any], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    units = manifest["units"]
    blocked = manifest["blocked"]
    unit_by_id = {unit["unit_id"]: results.get(unit["unit_id"], {"status": "pending"}) for unit in units}
    unit_counts = {state: 0 for state in ("ok", "error", "timeout", "pending", "stale", "blocked")}
    for item in unit_by_id.values():
        state = item.get("status", "pending")
        unit_counts[state] = unit_counts.get(state, 0) + 1

    blocked_files = {(item["project_id"], file_name) for item in blocked for file_name in item["files"]}
    successful_files = {
        (unit["project_id"], file_name)
        for unit in units
        if unit_by_id[unit["unit_id"]].get("status") == "ok"
        for file_name in unit["files"]
    }
    inventory = [(item["project_id"], item["path"]) for item in manifest["inventory_files"]]
    file_states: dict[tuple[str, str], str] = {}
    for key in inventory:
        if key in successful_files:
            file_states[key] = "ok"
        elif key in blocked_files:
            file_states[key] = "blocked"
        else:
            project, file_name = key
            related = [unit_by_id[unit["unit_id"]].get("status", "pending") for unit in units if unit["project_id"] == project and file_name in unit["files"]]
            file_states[key] = "error" if any(state in {"error", "timeout"} for state in related) else "pending"
    file_counts = {state: sum(value == state for value in file_states.values()) for state in ("ok", "blocked", "error", "pending")}

    projects = sorted({item["project_id"] for item in manifest["inventory_files"]} | {unit["project_id"] for unit in units} | {item["project_id"] for item in blocked})
    project_states: dict[str, str] = {}
    for project in projects:
        project_units = [unit_by_id[unit["unit_id"]] for unit in units if unit["project_id"] == project]
        project_files = [key for key in inventory if key[0] == project]
        if any(item["project_id"] == project for item in blocked):
            state = "blocked"
        elif any(item.get("status") in {"error", "timeout"} for item in project_units):
            state = "error"
        elif any(item.get("status") in {"pending", "stale"} for item in project_units) or any(file_states.get(key) == "pending" for key in project_files):
            state = "pending"
        elif project_units and all(item.get("status") == "ok" for item in project_units) and all(file_states.get(key) == "ok" for key in project_files):
            state = "ok"
        else:
            state = "pending"
        project_states[project] = state
    project_counts = {state: sum(value == state for value in project_states.values()) for state in ("ok", "blocked", "error", "pending")}
    return {
        "units": {"total": len(units), **unit_counts},
        "files": {"total": len(inventory), **file_counts},
        "projects": {"total": len(projects), **project_counts},
    }


def _summary(manifest: dict[str, Any], results: dict[str, dict[str, Any]], started: float, cache_hits: int, cache_misses: int) -> dict[str, Any]:
    coverage = _coverage(manifest, results)
    blocked = list(manifest["blocked"])
    blocked.extend(
        {
            "project_id": result.get("project_id"),
            "files": result.get("files", []),
            "stage": result.get("stage", "runner"),
            "error": result.get("error", f"unit status {result.get('status')}"),
            "unit_id": result.get("unit_id"),
        }
        for result in results.values()
        if result.get("status") not in {"ok"}
    )
    complete = not blocked and coverage["units"]["ok"] == coverage["units"]["total"] and coverage["files"]["ok"] == coverage["files"]["total"] and coverage["projects"]["blocked"] == 0 and coverage["projects"]["error"] == 0 and coverage["projects"]["pending"] == 0
    elapsed = round(time.monotonic() - started, 3)
    return {
        "schema": "spider-project-batch-summary/1",
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "complete": complete,
        "snapshot_commit": manifest["snapshot_commit"],
        "coverage": coverage,
        "blocked": blocked,
        "results": [results[key] for key in sorted(results)],
        "cache": {"hits": cache_hits, "misses": cache_misses},
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "peak_memory": None,
        "peak_memory_supported": False,
        "timings": {"seconds": elapsed},
    }


def _run_manifest(manifest_path: Path, output: Path, workers: int = 2, timeout: float = 600, resume: bool = False) -> dict[str, Any]:
    """Run every unit in a project manifest and return the persisted summary."""
    manifest_path = Path(manifest_path).resolve()
    output = Path(output).resolve()
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout must be positive")
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = _validate_manifest(_read_json(manifest_path))
    if output.exists() and not output.is_dir():
        raise ValueError(f"output is not a directory: {output}")
    if output.is_dir() and any(p.name != ".run.lock" for p in output.iterdir()) and not resume:
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    runtime = _runtime_signature()
    manifest_hash = digest(manifest)
    checkpoint = _load_checkpoint(output / "checkpoint.json") if resume else {}
    previous = {item.get("unit_id"): item for item in checkpoint.get("results", []) if isinstance(item, dict) and isinstance(item.get("unit_id"), str)}
    results: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    compatible_runtime = _runtime_compatible(checkpoint.get("runtime_signature"), runtime)
    for unit in manifest["units"]:
        reused = _validate_cached_result(unit, previous.get(unit["unit_id"], {}), output, runtime, compatible_runtime) if resume else None
        if reused is not None and checkpoint.get("manifest_digest") == manifest_hash:
            results[unit["unit_id"]] = reused
            cache_hits += 1
        else:
            pending.append(unit)
            cache_misses += 1
    _write_checkpoint(output, manifest_path, manifest_hash, manifest, runtime, results)

    started = time.monotonic()
    max_workers = min(_MAX_WORKERS, max(1, workers), max(1, len(pending)))
    if pending:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures: dict[Future[dict[str, Any]], dict[str, Any]] = {
                pool.submit(_run_unit, unit, output, runtime, float(timeout), 1): unit
                for unit in pending
            }
            for future in as_completed(futures):
                unit = futures[future]
                try:
                    result = future.result()
                except BaseException as error:
                    result = _result_base(unit, output, runtime, 1)
                    result.update(status="error", stage="runner", error=f"{type(error).__name__}: {error}")
                results[unit["unit_id"]] = result
                _write_checkpoint(output, manifest_path, manifest_hash, manifest, runtime, results)

    timed_out = [unit for unit in pending if results.get(unit["unit_id"], {}).get("status") == "timeout"]
    for unit in timed_out:
        result = _run_unit(unit, output, runtime, _RETRY_TIMEOUT, 2)
        results[unit["unit_id"]] = result
        _write_checkpoint(output, manifest_path, manifest_hash, manifest, runtime, results)

    final = _summary(manifest, results, started, cache_hits, cache_misses)
    write_json(output / "summary.json", final)
    _write_checkpoint(output, manifest_path, manifest_hash, manifest, runtime, results)
    return final


def run_manifest(manifest_path: Path, output: Path, workers: int = 2, timeout: float = 600, resume: bool = False) -> dict[str, Any]:
    """Hold an OS-released lock so interruption never leaves a stale PID lock."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".run.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            return _run_manifest(manifest_path, output, workers, timeout, resume)
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


__all__ = ["run_manifest"]
