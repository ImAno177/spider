from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from spider import project_batch
from spider.compilation import SCHEMA, digest, write_json
from spider.solc import compiler_fingerprint


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "A.sol"
    source.write_bytes(b"pragma solidity 0.4.25; contract A {}")
    plan = {
        "schema": SCHEMA,
        "project_id": "project-a",
        "sources": {"A.sol": {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}},
        "entries": ["A.sol"],
        "settings": {},
        "compiler": compiler_fingerprint("0.4.25"),
        "dependencies": [],
    }
    plan["unit_id"] = digest(plan)
    plan_path = tmp_path / "plan.json"
    write_json(plan_path, plan)
    manifest_path = tmp_path / "manifest.json"
    write_json(
        manifest_path,
        {
            "schema": "spider-project-manifest/1",
            "snapshot_commit": "fixture",
            "units": [{"unit_id": plan["unit_id"], "project_id": "project-a", "plan": str(plan_path), "files": ["A.sol"]}],
            "blocked": [],
            "inventory_files": [{"path": "A.sol", "project_id": "project-a", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
        },
    )
    return manifest_path, tmp_path / "output", plan["unit_id"]


def test_rejects_duplicate_or_unsafe_unit_ids(tmp_path: Path) -> None:
    manifest, output, unit_id = _fixture(tmp_path)
    value = project_batch._read_json(manifest)
    value["units"].append(dict(value["units"][0]))
    write_json(manifest, value)
    with pytest.raises(ValueError, match="duplicate unit_id"):
        project_batch.run_manifest(manifest, output)

    value["units"][-1]["unit_id"] = "../unsafe"
    write_json(manifest, value)
    with pytest.raises(ValueError, match="unsafe unit_id"):
        project_batch.run_manifest(manifest, output)


def test_independent_verifier_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        project_batch,
        "_run_process",
        lambda command, timeout, cwd: {"returncode": -9, "stdout": b"", "stderr": b"", "timeout": True, "seconds": timeout},
    )
    assert project_batch._verify_graph(tmp_path / "graph.json", 12, tmp_path) == [
        "independent verifier timed out after 12s"
    ]


def test_runner_only_runtime_change_keeps_cache_compatible() -> None:
    previous = {"tools": {"spider": "x"}, "code_sha256": {"verify.py": "a", "project_batch.py": "old"}}
    current = {"tools": {"spider": "x"}, "code_sha256": {"verify.py": "a", "project_batch.py": "new"}}
    assert project_batch._runtime_compatible(previous, current)


def test_resume_rejects_mutated_artifact_and_reruns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, output, unit_id = _fixture(tmp_path)
    calls: list[int] = []

    def fake_run(unit: dict, destination: Path, runtime: dict, timeout: float, attempt: int = 1) -> dict:
        calls.append(attempt)
        root = destination / "units" / unit["unit_id"]
        compilation = root / "graph.compilation"
        compilation.mkdir(parents=True, exist_ok=True)
        write_json(root / "graph.json", {"graph": {"compilation_unit_id": unit["unit_id"]}, "nodes": [], "links": []})
        write_json(compilation / "status.json", {"solc_ok": True, "slither_ok": True, "graph_built": True, "graph_valid": True})
        result = project_batch._result_base(unit, destination, runtime, attempt)
        result.update(
            status="ok",
            stage="complete",
            stages={key: True for key in project_batch._STAGE_KEYS},
            returncode=0,
            seconds=0.001,
            graph_sha256=project_batch._sha256(root / "graph.json"),
            artifact_checksums=project_batch._artifact_checksums(root),
        )
        return result

    monkeypatch.setattr(project_batch, "_run_unit", fake_run)
    first = project_batch.run_manifest(manifest, output)
    assert first["status"] == "COMPLETE"
    assert calls == [1]

    graph = output / "units" / unit_id / "graph.json"
    graph.write_text(graph.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    second = project_batch.run_manifest(manifest, output, resume=True)
    assert second["status"] == "COMPLETE"
    assert second["cache_hits"] == 0
    assert second["cache_misses"] == 1
    assert calls == [1, 1]


def test_real_compile_and_resume(tmp_path: Path) -> None:
    manifest, output, _ = _fixture(tmp_path)
    first = project_batch.run_manifest(manifest, output)
    assert first["status"] == "COMPLETE"
    second = project_batch.run_manifest(manifest, output, resume=True)
    assert second["cache_hits"] == 1
