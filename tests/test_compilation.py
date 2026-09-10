import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from spider.compilation import SCHEMA, _normalize_source_asts, _slither_recursion_budget, digest, extract_plan, load_plan, write_json
from spider.solc import compiler_fingerprint
from spider.verify import validate


def test_slither_recursion_budget_is_temporary(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "getrecursionlimit", lambda: 1000)
    monkeypatch.setattr(sys, "setrecursionlimit", calls.append)
    with _slither_recursion_budget():
        pass
    assert calls == [3000, 1000]


def test_legacy_solc_ast_is_promoted_without_overwriting_ast():
    legacy = {"nodeType": "SourceUnit", "nodes": []}
    current = {"nodeType": "SourceUnit", "nodes": [{"nodeType": "PragmaDirective"}]}
    output = {"sources": {"legacy.sol": {"legacyAST": legacy}, "current.sol": {"ast": current, "legacyAST": legacy}}}

    _normalize_source_asts(output)

    assert output["sources"]["legacy.sol"]["ast"] is legacy
    assert output["sources"]["current.sol"]["ast"] is current


def test_legacy_solc_import_path_uses_source_unit_name():
    output = {
        "sources": {
            "contracts/v2/Token.sol": {
                "legacyAST": {
                    "name": "SourceUnit",
                    "children": [{"name": "ImportDirective", "attributes": {"file": "../v1/Base.sol"}}],
                }
            },
            "contracts/v1/Base.sol": {"legacyAST": {"name": "SourceUnit", "children": []}},
        }
    }

    _normalize_source_asts(output)

    assert output["sources"]["contracts/v2/Token.sol"]["ast"]["children"][0]["attributes"]["absolutePath"] == "contracts/v1/Base.sol"


def test_plan_compile_and_mutation(tmp_path: Path):
    source = tmp_path / "A.sol"
    source.write_bytes(b"pragma solidity 0.4.25; contract A { uint public x; function set(uint v) public { x = v; } }")
    plan = {"schema": SCHEMA, "project_id": "audit/project", "sources": {"A.sol": {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}},
            "entries": ["A.sol"], "settings": {}, "compiler": compiler_fingerprint("0.4.25"), "dependencies": []}
    plan["unit_id"] = digest(plan)
    path = tmp_path / "plan.json"
    write_json(path, plan)
    assert load_plan(path) == plan
    graph = extract_plan(path, tmp_path / "compilation")
    assert not validate(graph)
    assert (tmp_path / "compilation/output.json").is_file()
    assert extract_plan(path, tmp_path / "compilation") == graph
    assert json.loads((tmp_path / "compilation/status.json").read_text())["compiler_cache_hit"]
    (tmp_path / "compilation/output.json").write_text("{}")
    assert extract_plan(path, tmp_path / "compilation") == graph
    assert not json.loads((tmp_path / "compilation/status.json").read_text())["compiler_cache_hit"]
    broken = deepcopy(graph)
    broken["graph"]["compilation_plan"]["settings"]["viaIR"] = True
    assert "compilation plan digest mismatch" in validate(broken)
    broken = deepcopy(graph)
    broken["graph"]["source_files"][0]["sha256"] = "0" * 64
    assert "compilation plan source manifest mismatch" in validate(broken)
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="source hash"):
        load_plan(path)


def test_solc_failure_preserves_diagnostics(tmp_path: Path):
    source = tmp_path / "Bad.sol"
    source.write_bytes(b"pragma solidity 0.4.25; contract A { invalid syntax }")
    plan = {"schema": SCHEMA, "project_id": "audit/bad", "sources": {"Bad.sol": {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}},
            "entries": ["Bad.sol"], "settings": {}, "compiler": compiler_fingerprint("0.4.25"), "dependencies": []}
    plan["unit_id"] = digest(plan)
    path = tmp_path / "plan.json"
    write_json(path, plan)
    with pytest.raises(ValueError):
        extract_plan(path, tmp_path / "compilation")
    status = json.loads((tmp_path / "compilation/status.json").read_text())
    assert status["stage"] == "solc" and not status["solc_ok"] and not status["graph_valid"]
    assert (tmp_path / "compilation/output.json").is_file()


def test_compiler_candidates_fallback_records_selected_release(tmp_path: Path):
    source = tmp_path / "Fallback.sol"
    source.write_text(
        "pragma solidity >=0.7.0 <0.9.0; contract A { function f(uint x) public pure returns (uint) { unchecked { return x + 1; } } }",
        encoding="utf-8",
    )
    plan = {
        "schema": SCHEMA,
        "project_id": "audit/fallback",
        "sources": {"Fallback.sol": {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}},
        "entries": ["Fallback.sol"],
        "settings": {},
        "compiler": compiler_fingerprint("0.7.6"),
        "compiler_candidates": ["0.7.6", "0.8.25"],
        "dependencies": [],
    }
    plan["unit_id"] = digest(plan)
    path = tmp_path / "fallback-plan.json"
    write_json(path, plan)
    graph = extract_plan(path, tmp_path / "compilation")
    assert graph["graph"]["solc_version"] == "0.8.25"
    assert graph["graph"]["compiler_selection"]["selected"]["requested"] == "0.8.25"
    assert len(graph["graph"]["compiler_selection"]["attempts"]) == 2
    assert not validate(graph)


def test_optimizer_recovery_records_unpinned_stack_fix(tmp_path: Path):
    parameters = ", ".join(f"bytes memory a{i}" for i in range(12))
    arguments = ", ".join(f"a{i}" for i in range(12))
    source = tmp_path / "OptimizerRecovery.sol"
    source.write_text(
        "pragma solidity 0.6.12; pragma experimental ABIEncoderV2; "
        f"contract OptimizerRecovery {{ function encode({parameters}) public pure returns (bytes memory) "
        f"{{ return abi.encode({arguments}); }} }}",
        encoding="utf-8",
    )
    plan = {
        "schema": SCHEMA,
        "project_id": "audit/optimizer-recovery",
        "sources": {"OptimizerRecovery.sol": {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}},
        "entries": ["OptimizerRecovery.sol"],
        "settings": {},
        "compiler": compiler_fingerprint("0.6.12"),
        "dependencies": [],
    }
    plan["unit_id"] = digest(plan)
    path = tmp_path / "optimizer-plan.json"
    write_json(path, plan)

    graph = extract_plan(path, tmp_path / "compilation")
    attempts = graph["graph"]["compiler_selection"]["attempts"]
    assert attempts[0]["success"] is False
    assert attempts[1]["recovery"] == "optimizer"
    assert attempts[1]["success"] is True
    assert graph["graph"]["compiler_selection"]["selected_settings"]["optimizer"] == {"enabled": True, "runs": 200}
    assert not validate(graph)
