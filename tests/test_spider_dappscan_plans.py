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


def test_infer_local_monorepo_package_remapping_requires_complete_target(tmp_path):
    spec = importlib.util.spec_from_file_location("dappscan_plans", Path(__file__).parents[1] / "scripts/spider_dappscan_plans.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "sources/audit/project"
    (project / "contracts/utils/contracts/src").mkdir(parents=True)
    (project / "contracts/utils/contracts/src/Lib.sol").write_text("pragma solidity 0.4.25; library Lib {}", encoding="utf-8")
    (project / "contracts/App.sol").write_text(
        'pragma solidity 0.4.25; import "@0x/contracts-utils/contracts/src/Lib.sol"; contract App {}',
        encoding="utf-8",
    )
    inferred, evidence, warnings = module.infer_local_package_remappings(project, [])
    assert inferred == ["@0x/contracts-utils/=contracts/utils/"]
    assert evidence[0]["kind"] == "dappscan-local-package-layout"
    assert warnings == []
    assert module.infer_local_package_remappings(project, inferred)[0] == []


def test_locked_external_dependency_is_virtualized_in_plan(tmp_path):
    spec = importlib.util.spec_from_file_location("dappscan_plans", Path(__file__).parents[1] / "scripts/spider_dappscan_plans.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source_root = tmp_path / "sources"
    project = source_root / "audit/project"
    project.mkdir(parents=True)
    dependency = tmp_path / "deps/openzeppelin-solidity-2.3.0"
    (dependency / "contracts/math").mkdir(parents=True)
    (dependency / "contracts/math/SafeMath.sol").write_text(
        "pragma solidity ^0.4.24; library SafeMath {}", encoding="utf-8"
    )
    source = project / "A.sol"
    source.write_text(
        'pragma solidity 0.4.25; import "openzeppelin-solidity-2.3.0/contracts/math/SafeMath.sol"; contract A {}',
        encoding="utf-8",
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "source_root": str(source_root),
                "commit": "fixed",
                "files": [
                    {
                        "path": "audit/project/A.sol",
                        "project_id": "audit/project",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "kind": "solidity",
                    }
                ],
            }
        )
    )
    lock = tmp_path / "lock.json"
    archive = tmp_path / "dependency.tgz"
    archive.write_bytes(b"locked dependency")
    lock.write_text(
        json.dumps(
            {
                "schema": "spider-dappscan-dependency-lock/1",
                "entries": [
                    {
                        "id": "npm:openzeppelin-solidity@2.3.0",
                        "prefix": "openzeppelin-solidity-2.3.0/",
                        "root": str(dependency),
                        "archive": str(archive),
                        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "integrity": "sha512-test",
                    }
                ],
            }
        )
    )
    summary = module.build(inventory, tmp_path / "plans", lock)
    assert summary == {"files": 1, "units": 1, "blocked": 0}
    plan = json.loads(next((tmp_path / "plans/plans").glob("*.json")).read_text())
    assert "dependencies/npm-openzeppelin-solidity-2.3.0/contracts/math/SafeMath.sol" in plan["sources"]
    assert "openzeppelin-solidity-2.3.0/=dependencies/npm-openzeppelin-solidity-2.3.0/" in plan["settings"]["remappings"]
    assert plan["dependencies"][0]["id"] == "npm:openzeppelin-solidity@2.3.0"


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
