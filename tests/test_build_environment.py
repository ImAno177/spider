import hashlib
import json
from pathlib import Path

import pytest

from spider.build_environment import discover_build


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _build_info(project: Path, source: Path, *, version: str = "0.8.20", settings: dict | None = None) -> Path:
    name = source.relative_to(project).as_posix()
    payload = {
        "_format": "hh-sol-build-info-1",
        "solcVersion": version,
        "input": {
            "language": "Solidity",
            "sources": {name: {"content": source.read_bytes().decode("utf-8")}},
            "settings": settings or {},
        },
        "output": {"contracts": {}, "sources": {}},
    }
    path = project / "artifacts" / "build-info" / f"{version.replace('.', '-')}-{hashlib.sha256(name.encode()).hexdigest()[:8]}.json"
    _write(path, json.dumps(payload))
    return path


def test_discover_verified_hardhat_build_info(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = _write(project / "contracts" / "A.sol", "pragma solidity ^0.8.0; contract A {}\n")
    settings = {"optimizer": {"enabled": True, "runs": 200}, "remappings": ["@dep/=node_modules/@dep/"]}
    build_info = _build_info(project, source, settings=settings)

    result = discover_build(project)

    assert result["settings"] == settings
    assert result["compiler_version"] == "0.8.20"
    assert result["remappings"] == settings["remappings"]
    assert result["origin"] == "hardhat-build-info"
    assert result["warnings"] == []
    assert result["evidence"][0]["path"] == str(build_info.resolve())
    assert result["evidence"][0]["sha256"] == hashlib.sha256(build_info.read_bytes()).hexdigest()


def test_stale_build_info_is_rejected_with_warning(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = _write(project / "contracts" / "A.sol", "contract A {}\n")
    _build_info(project, source)
    source.write_text("contract Changed {}\n", encoding="utf-8")

    result = discover_build(project)

    assert result["origin"] == "reconstructed-fallback"
    assert result["compiler_version"] is None
    assert any("stale build-info source content mismatch" in warning for warning in result["warnings"])


def test_foundry_default_profile_and_remappings_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(
        project / "foundry.toml",
        """
[profile.default]
solc = "0.8.23"
optimizer = true
optimizer_runs = 500
evm_version = "paris"
via_ir = true
""".lstrip(),
    )
    remappings = _write(project / "remappings.txt", "@dep/=lib/dep/\n# ignored\n")

    result = discover_build(project)

    assert result["origin"] == "foundry.toml"
    assert result["compiler_version"] == "0.8.23"
    assert result["remappings"] == ["@dep/=lib/dep/"]
    assert result["settings"] == {
        "optimizer": {"enabled": True, "runs": 500},
        "evmVersion": "paris",
        "viaIR": True,
        "remappings": ["@dep/=lib/dep/"],
    }
    assert any(item["path"] == str(remappings.resolve()) for item in result["evidence"])


def test_dynamic_config_is_reported_without_execution(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(project / "hardhat.config.js", "throw new Error('must not execute');\n")

    result = discover_build(project)

    assert result["origin"] == "reconstructed-fallback"
    assert any("dynamic build configuration was not executed" in warning for warning in result["warnings"])
    assert "hardhat.config.js" in " ".join(result["warnings"])


def test_override_requires_evidence_and_wins_over_stale_build_info(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = _write(project / "contracts" / "A.sol", "contract A {}\n")
    _build_info(project, source, version="0.8.19")
    override = {
        "compiler_version": "0.8.20",
        "settings": {"viaIR": True},
        "remappings": ["@dep/=vendor/dep/"],
        "origin": "locked-manifest",
        "evidence": ["manifest.json sha256:abc"],
    }

    result = discover_build(project, override)

    assert result["compiler_version"] == "0.8.20"
    assert result["settings"] == {"viaIR": True}
    assert result["remappings"] == ["@dep/=vendor/dep/"]
    assert result["origin"] == "locked-manifest"
    assert result["evidence"] == override["evidence"]
    with pytest.raises(ValueError, match="evidence is required"):
        discover_build(project, {**override, "evidence": []})


def test_incompatible_verified_build_infos_are_ambiguous(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = _write(project / "contracts" / "A.sol", "contract A {}\n")
    _build_info(project, source, version="0.8.19")
    _build_info(project, source, version="0.8.20", settings={"optimizer": {"enabled": True}})

    with pytest.raises(ValueError, match="AMBIGUOUS_BUILD_CONFIGURATION"):
        discover_build(project)
