from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.spider_dappscan_inventory import (
    SCHEMA,
    _extract_contracts,
    _git_blob_sha1,
    inventory_snapshot,
)


def _write(path: Path, text: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return data


def test_inventory_hashes_solidity_imports_and_configs(tmp_path: Path) -> None:
    root = tmp_path / "contracts"
    main = _write(
        root / "Audit" / "Project" / "contracts" / "Main.sol",
        "// import \"fake.sol\";\npragma solidity ^0.8.0;\nimport \"../Lib.sol\";\ncontract Main {}\n",
    )
    lib = _write(root / "Audit" / "Project" / "Lib.sol", "pragma solidity >=0.7.0 <0.9.0;\nlibrary Lib {}\n")
    _write(root / "Audit" / "Project" / "foundry.toml", "[profile.default]\nsolc_version = '0.8.0'\n")
    output = tmp_path / "inventory.json"
    remote = {
        "Audit/Project/contracts/Main.sol": {"mode": "100644", "type": "blob", "sha": _git_blob_sha1(main)},
        "Audit/Project/Lib.sol": {"mode": "100644", "type": "blob", "sha": _git_blob_sha1(lib)},
        "Audit/Project/foundry.toml": {
            "mode": "100644",
            "type": "blob",
            "sha": _git_blob_sha1((root / "Audit" / "Project" / "foundry.toml").read_bytes()),
        },
    }
    result = inventory_snapshot(root, output, commit="fixture", remote_entries=remote)

    assert result["schema"] == SCHEMA
    assert result["project_count"] == 1
    assert result["solidity_file_count"] == 2
    assert result["config_file_count"] == 1
    assert result["errors"] == []
    main_record = next(item for item in result["files"] if item["path"].endswith("Main.sol"))
    assert main_record["project_id"] == "Audit/Project"
    assert main_record["imports"] == ["../Lib.sol"]
    assert main_record["pragmas"] == ["^ 0 . 8 . 0"]
    assert main_record["bytes"] == len(main)
    assert main_record["sha256"] == hashlib.sha256(main).hexdigest()
    serialized = json.loads(output.read_text(encoding="utf-8"))
    assert serialized["file_count"] == 2
    assert serialized["all_file_count"] == 3


def test_inventory_records_lfs_and_missing_remote_files(tmp_path: Path) -> None:
    root = tmp_path / "contracts"
    pointer = _write(
        root / "Audit" / "Project" / "Missing.sol",
        "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 3\n",
    )
    remote = {
        "Audit/Project/Missing.sol": {"mode": "100644", "type": "blob", "sha": _git_blob_sha1(pointer)},
        "Audit/Project/NotDownloaded.sol": {"mode": "100644", "type": "blob", "sha": "deadbeef"},
    }
    result = inventory_snapshot(root, tmp_path / "inventory.json", commit="fixture", remote_entries=remote)

    assert result["solidity_file_count"] == 2
    assert result["file_count"] == 2
    assert any(item["error"] == "git_lfs_pointer" for item in result["errors"])
    missing = next(item for item in result["files"] if item["path"].endswith("NotDownloaded.sol"))
    assert missing["bytes"] is None
    assert "missing_from_snapshot" in missing["errors"]


def test_extract_contracts_rejects_traversal_without_writing_outside(tmp_path: Path) -> None:
    archive = tmp_path / "fixture.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        payload = b"contract Safe {}\n"
        info = tarfile.TarInfo("DAppSCAN-fixture/DAppSCAN-source/contracts/A.sol")
        info.size = len(payload)
        handle.addfile(info, io.BytesIO(payload))
        bad = tarfile.TarInfo("DAppSCAN-fixture/DAppSCAN-source/contracts/../escape.sol")
        bad.size = len(payload)
        handle.addfile(bad, io.BytesIO(payload))
    destination = tmp_path / "extracted"
    errors = _extract_contracts(archive, destination)

    assert (destination / "A.sol").read_bytes() == b"contract Safe {}\n"
    assert not (tmp_path / "escape.sol").exists()
    assert any(item["error"] == "unsafe_archive_path" for item in errors)


def test_inventory_rejects_nonexistent_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        inventory_snapshot(tmp_path / "missing", tmp_path / "inventory.json", commit="fixture")
