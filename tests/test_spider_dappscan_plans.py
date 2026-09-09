import hashlib
import importlib.util
import json
from pathlib import Path


def test_plan_inventory_keeps_blocked_sources(tmp_path):
    spec = importlib.util.spec_from_file_location("dappscan_plans", Path(__file__).parents[1] / "scripts/spider_dappscan_plans.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "sources/audit/project"
    project.mkdir(parents=True)
    files = []
    for name, code in {"A.sol": "pragma solidity 0.4.25; contract A {}", "B.sol": 'pragma solidity 0.4.25; import "absent.sol";'}.items():
        path = project / name
        path.write_bytes(code.encode())
        files.append({"path": "audit/project/" + name, "project_id": "audit/project", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"source_root": str(tmp_path / "sources"), "commit": "fixed", "files": files}))
    summary = module.build(inventory, tmp_path / "plans")
    assert summary == {"files": 2, "units": 1, "blocked": 1}
    manifest = json.loads((tmp_path / "plans/manifest.json").read_text())
    assert len(manifest["inventory_files"]) == 2
    assert manifest["blocked"][0]["stage"] == "dependency"
    assert "MISSING_DEPENDENCY" in manifest["blocked"][0]["error"]


def test_plan_uses_verified_foundry_settings(tmp_path):
    spec = importlib.util.spec_from_file_location("dappscan_plans", Path(__file__).parents[1] / "scripts/spider_dappscan_plans.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "sources/audit/project"
    project.mkdir(parents=True)
    (project / "foundry.toml").write_text("[profile.default]\nsolc = '0.4.25'\noptimizer = true\noptimizer_runs = 77\n", encoding="utf-8")
    source = project / "A.sol"
    source.write_text("pragma solidity 0.4.25; contract A {}", encoding="utf-8")
    record = {"path": "audit/project/A.sol", "project_id": "audit/project", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "kind": "solidity"}
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"source_root": str(tmp_path / "sources"), "commit": "fixed", "files": [record]}))
    summary = module.build(inventory, tmp_path / "plans")
    assert summary["units"] == 1 and summary["blocked"] == 0
    plan_path = next((tmp_path / "plans/plans").glob("*.json"))
    plan = json.loads(plan_path.read_text())
    assert plan["compiler"]["requested"] == "0.4.25"
    assert plan["settings"]["optimizer"] == {"enabled": True, "runs": 77}
    assert plan["environment_origin"] == "foundry.toml"


def test_unpinned_plan_prefers_newest_compatible_compiler(tmp_path):
    spec = importlib.util.spec_from_file_location("dappscan_plans", Path(__file__).parents[1] / "scripts/spider_dappscan_plans.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "sources/audit/project"
    project.mkdir(parents=True)
    source = project / "A.sol"
    source.write_text("pragma solidity >=0.5.0 <0.8.0; contract A {}", encoding="utf-8")
    record = {"path": "audit/project/A.sol", "project_id": "audit/project", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "kind": "solidity"}
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"source_root": str(tmp_path / "sources"), "commit": "fixed", "files": [record]}))
    summary = module.build(inventory, tmp_path / "plans")
    assert summary["units"] == 1 and summary["blocked"] == 0
    plan = json.loads(next((tmp_path / "plans/plans").glob("*.json")).read_text())
    assert plan["compiler"]["requested"] == "0.7.6"
