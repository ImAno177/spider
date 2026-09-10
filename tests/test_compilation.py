import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

import spider.compilation as compilation
from spider.compilation import (
    SCHEMA,
    _compile_candidates,
    _normalize_compiler_sources,
    _normalize_legacy_combined_output,
    _normalize_source_asts,
    _slither_recursion_budget,
    digest,
    extract_plan,
    load_plan,
    write_json,
)
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


def test_legacy_combined_output_matches_standard_json_shape():
    output = {
        "version": "0.4.10+commit.9e8cc01b.Linux.g++",
        "sourceList": ["contracts/v2/Token.sol", "contracts/v1/Base.sol"],
        "sources": {
            "contracts/v2/Token.sol": {
                "AST": {
                    "name": "SourceUnit",
                    "children": [
                        {
                            "name": "ImportDirective",
                            "attributes": {"file": "../v1/Base.sol"},
                            "src": "0:21:1",
                        }
                    ],
                }
            },
            "contracts/v1/Base.sol": {"AST": {"name": "SourceUnit", "children": []}},
        },
        "contracts": {
            "contracts/v2/Token.sol:Token": {
                "abi": "[]",
                "bin": "6000",
                "bin-runtime": "00",
                "srcmap": "0:1:1",
                "srcmap-runtime": "0:1:1",
            }
        },
    }

    _normalize_legacy_combined_output(output)

    token_ast = output["sources"]["contracts/v2/Token.sol"]["ast"]
    base_ast = output["sources"]["contracts/v1/Base.sol"]["ast"]
    assert token_ast["src"] == "0:0:1"
    assert base_ast["src"] == "0:0:2"
    assert token_ast["children"][0]["attributes"]["absolutePath"] == "contracts/v1/Base.sol"
    contract = output["contracts"]["contracts/v2/Token.sol"]["Token"]
    assert contract["abi"] == []
    assert contract["evm"] == {
        "bytecode": {"object": "6000", "sourceMap": "0:1:1"},
        "deployedBytecode": {"object": "00", "sourceMap": "0:1:1"},
    }


def test_legacy_compiler_uses_combined_json(monkeypatch, tmp_path: Path):
    calls = []
    combined = {
        "version": "0.4.10+commit.9e8cc01b.Linux.g++",
        "sourceList": ["A.sol"],
        "sources": {"A.sol": {"AST": {"name": "SourceUnit", "children": []}}},
        "contracts": {"A.sol:A": {"abi": "[]", "bin": "", "bin-runtime": "", "srcmap": "", "srcmap-runtime": ""}},
    }

    class Result:
        returncode = 0
        stdout = json.dumps(combined).encode()
        stderr = b""

    fingerprint = {"requested": "0.4.10", "reported": "0.4.10+commit.9e8cc01b.Linux.g++", "binary_sha256": "test", "usable": True}

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(compilation, "compiler_command", lambda version, *arguments: ["solc", *arguments])
    monkeypatch.setattr(compilation, "compiler_fingerprint", lambda version: fingerprint)
    monkeypatch.setattr(compilation.subprocess, "run", fake_run)

    plan = {"compiler_candidates": ["0.4.10"], "compiler": {"requested": "0.4.10"}, "settings": {}}
    standard = {"language": "Solidity", "sources": {"A.sol": {"content": "contract A {}"}}, "settings": {}}
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    version, output, _, attempts, _ = _compile_candidates(plan, standard, tmp_path, artifacts)

    assert version == "0.4.10"
    assert attempts[0]["success"]
    assert calls[0][0] == ["solc", "--combined-json", "abi,ast,bin,bin-runtime,srcmap,srcmap-runtime", "A.sol"]
    assert calls[0][1]["input"] is None
    assert attempts[0]["source_normalization"] == {"schema": "spider-source-normalization/1", "applied": False, "removed": []}
    assert output["sources"]["A.sol"]["ast"]["src"] == "0:0:1"


def test_legacy_compiler_failure_keeps_stderr_diagnostics(monkeypatch, tmp_path: Path):
    class Result:
        returncode = 1
        stdout = b""
        stderr = b"A.sol:1:1: Error: parser failure\n"

    fingerprint = {"requested": "0.4.10", "reported": "0.4.10+commit.9e8cc01b.Linux.g++", "binary_sha256": "test", "usable": True}

    monkeypatch.setattr(compilation, "compiler_command", lambda version, *arguments: ["solc", *arguments])
    monkeypatch.setattr(compilation, "compiler_fingerprint", lambda version: fingerprint)
    monkeypatch.setattr(compilation.subprocess, "run", lambda *args, **kwargs: Result())

    plan = {"compiler_candidates": ["0.4.10"], "compiler": {"requested": "0.4.10"}, "settings": {}}
    standard = {"language": "Solidity", "sources": {"A.sol": {"content": "contract A {}"}}, "settings": {}}
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    with pytest.raises(ValueError, match="parser failure"):
        _compile_candidates(plan, standard, tmp_path, artifacts)
    attempts = json.loads((artifacts / "compiler-attempts.json").read_text())
    assert attempts[0]["diagnostics"] == ["A.sol:1:1: Error: parser failure"]
    assert attempts[0]["stderr"] == "A.sol:1:1: Error: parser failure"


def test_compiler_source_normalization_removes_only_leading_utf8_bom():
    standard = {"language": "Solidity", "sources": {"BOM.sol": {"content": "\ufeffcontract A {}"}, "plain.sol": {"content": "contract B {}"}}, "settings": {}}

    normalized, metadata = _normalize_compiler_sources(standard)

    assert standard["sources"]["BOM.sol"]["content"].startswith("\ufeff")
    assert normalized["sources"]["BOM.sol"]["content"] == "contract A {}"
    assert normalized["sources"]["plain.sol"] == standard["sources"]["plain.sol"]
    assert metadata == {
        "schema": "spider-source-normalization/1",
        "applied": True,
        "removed": [{"source_unit": "BOM.sol", "encoding": "utf-8-bom", "removed_bytes": 3}],
    }


def test_bom_plan_compiles_with_raw_manifest_and_normalization_metadata(tmp_path: Path):
    source = tmp_path / "BOM.sol"
    raw = b"\xef\xbb\xbfpragma solidity 0.8.25; contract A { uint public x; }"
    source.write_bytes(raw)
    plan = {
        "schema": SCHEMA,
        "project_id": "audit/bom",
        "sources": {"BOM.sol": {"path": str(source), "sha256": hashlib.sha256(raw).hexdigest()}},
        "entries": ["BOM.sol"],
        "settings": {},
        "compiler": compiler_fingerprint("0.8.25"),
        "dependencies": [],
    }
    plan["unit_id"] = digest(plan)
    plan_path = tmp_path / "bom-plan.json"
    artifact_path = tmp_path / "compilation"
    write_json(plan_path, plan)

    graph = extract_plan(plan_path, artifact_path)
    status = json.loads((artifact_path / "status.json").read_text(encoding="utf-8"))
    input_standard = json.loads((artifact_path / "input.json").read_text(encoding="utf-8"))

    assert input_standard["sources"]["BOM.sol"]["content"].startswith("pragma solidity")
    assert status["source_normalization"]["applied"] is True
    assert status["source_normalization"]["removed"][0]["source_unit"] == "BOM.sol"
    assert graph["graph"]["source_files"] == [
        {
            "file_id": 0,
            "path": str(artifact_path / "sources" / "BOM.sol").replace("\\", "/"),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_length": len(raw),
            "encoding": "utf-8",
        }
    ]
    assert graph["graph"]["source_normalization"] == status["source_normalization"]
    assert not validate(graph)


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
