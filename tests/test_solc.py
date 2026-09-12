import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import spider.solc as solc
from spider.solc import compatible_project_versions, compatible_version, compatible_versions, pragma_expressions, pragma_from, resolve_solc, solidity_sources


def test_solc_resolution() -> None:
    versions = [(0, 6, 12), (0, 8, 10), (0, 8, 20)]
    assert compatible_version("=0.8.10 >=0.8.0 <0.9.0", versions) == "0.8.10"
    assert compatible_version("^0.8.10", versions) == "0.8.20"
    assert compatible_versions("^0.8.10", versions) == ["0.8.20", "0.8.10"]
    assert compatible_version("0.7.6", versions) is None
    broad = [(0, 4, 18), (0, 4, 26), (0, 5, 17), (0, 8, 25)]
    assert compatible_versions(">=0.4.18", broad) == ["0.4.26", "0.4.18", "0.5.17", "0.8.25"]
    assert compatible_project_versions(["^0.8.10", ">=0.8.20 <0.9.0"], versions) == ["0.8.20"]
    selected, binary = resolve_solc(Path(__file__).parent / "fixtures" / "Bank.sol")
    assert selected and binary.is_file()


def test_pragma_ignores_comments(tmp_path: Path) -> None:
    source = tmp_path / "Commented.sol"
    source.write_text(
        "// pragma solidity 0.8.7;\n"
        "/* pragma solidity 0.7.4; */\n"
        "pragma solidity ^0.5.17;\n"
        "contract C { string constant S = \"// pragma solidity 0.8.0;\"; }\n",
        encoding="utf-8",
    )
    assert pragma_from(source) == "^0.5.17"


def test_all_pragmas_are_collected(tmp_path: Path) -> None:
    source = tmp_path / "Multiple.sol"
    source.write_text("pragma solidity ^0.6.0;\ncontract A {}\npragma solidity 0.6.6;\n", encoding="utf-8")
    assert pragma_expressions(source) == ["^0.6.0", "0.6.6"]


def test_project_source_discovery(tmp_path: Path) -> None:
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "A.sol").write_text("pragma solidity 0.8.20; contract A {}", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "Ignored.sol").write_text("contract Ignored {}", encoding="utf-8")
    assert solidity_sources(tmp_path) == [(tmp_path / "contracts" / "A.sol").resolve()]
    empty = tmp_path / "empty"
    empty.mkdir()
    try:
        solidity_sources(empty)
    except ValueError as error:
        assert "No Solidity sources" in str(error)
    else:
        raise AssertionError("empty project directory was accepted")


def test_compiler_fingerprint_rejects_prerelease_and_tracks_binary(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "solc-0.4.15"
    binary.write_bytes(b"nightly")
    output = "Version: 0.4.15-nightly.2017.8.10+commit.8b45bddb.Windows.msvc\n"
    monkeypatch.setattr(solc, "artifact_path", lambda _: binary)
    monkeypatch.setattr(
        solc.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
    )
    solc.compiler_fingerprint.cache_clear()
    nightly = solc.compiler_fingerprint("0.4.15")
    assert nightly["reported"].startswith("0.4.15-nightly") and not nightly["usable"]

    monkeypatch.setitem(solc._AUTHENTICATED_RELEASE_DIGESTS, "0.4.15", {nightly["binary_sha256"]})
    solc.compiler_fingerprint.cache_clear()
    authenticated = solc.compiler_fingerprint("0.4.15")
    assert authenticated["usable"]

    binary.write_bytes(b"stable")
    output = "Version: 0.4.15+commit.8b45bddb.Windows.msvc\n"
    solc.compiler_fingerprint.cache_clear()
    stable = solc.compiler_fingerprint("0.4.15")
    assert stable["reported"].startswith("0.4.15+commit") and stable["usable"]
    assert stable["binary_sha256"] != nightly["binary_sha256"]
    solc.compiler_fingerprint.cache_clear()


def test_explicit_0415_wsl_release_transport(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "solc-0.4.15-linux"
    binary.write_bytes(b"stable-linux")
    monkeypatch.setenv(solc._SOLC_0415_LINUX_ENV, str(binary))
    command = solc.compiler_command("0.4.15", "--version")
    if os.name == "nt":
        assert command[:2] == ["wsl.exe", "--"]
        assert command[2].startswith("/mnt/")
    else:
        assert command[0] == str(binary.resolve())
    solc.compiler_fingerprint.cache_clear()
    monkeypatch.setattr(
        solc.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "Version: 0.4.15+commit.8b45bddb.Linux.g++\n", ""),
    )
    fingerprint = solc.compiler_fingerprint("0.4.15")
    assert fingerprint["transport"] == "wsl"
    assert fingerprint["usable"] is True
    solc.compiler_fingerprint.cache_clear()


def test_explicit_0415_posix_absolute_release(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "solc-0.4.15-linux"
    binary.write_bytes(b"stable-linux")
    monkeypatch.setenv(solc._SOLC_0415_LINUX_ENV, str(binary))
    monkeypatch.setattr(solc, "os", SimpleNamespace(name="posix", environ=os.environ))
    command = solc.compiler_command("0.4.15", "--version")
    assert command == [str(binary.resolve()), "--version"]
    solc.compiler_fingerprint.cache_clear()
    monkeypatch.setattr(
        solc.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "Version: 0.4.15+commit.8b45bddb.Linux.g++\n", ""),
    )
    fingerprint = solc.compiler_fingerprint("0.4.15")
    assert fingerprint["transport"] == "native"
    assert fingerprint["usable"] is True
    solc.compiler_fingerprint.cache_clear()


def test_cli_and_batch_record_actual_compiler(tmp_path: Path) -> None:
    fixtures = Path(__file__).parent / "fixtures"
    cli_graph = tmp_path / "bank.json"
    calls_dot = tmp_path / "bank-calls.dot"
    xcfg_dot = tmp_path / "bank-xcfg.dot"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "spider",
            str(fixtures / "Bank.sol"),
            str(cli_graph),
            "--solc-version",
            "0.4.25",
            "--export",
            f"calls={calls_dot}",
            "--export",
            f"edge:XCFG_CALL={xcfg_dot}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(cli_graph.read_text(encoding="utf-8"))["graph"]["solc_version"] == "0.4.25"
    assert calls_dot.is_file() and "LOW_LEVEL_CALL" in calls_dot.read_text(encoding="utf-8")
    assert xcfg_dot.is_file()

    project_graph_path = tmp_path / "project.json"
    subprocess.run(
        [sys.executable, "-m", "spider", str(fixtures / "project"), str(project_graph_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    project_graph = json.loads(project_graph_path.read_text(encoding="utf-8"))
    assert project_graph["graph"]["input_kind"] == "directory"
    assert "XCFG_CALL" in project_graph["graph"]["edge_types"]

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(fixtures / "Control.sol", corpus / "Control.sol")
    review = tmp_path / "review"
    subprocess.run(
        [sys.executable, "-m", "spider.batch", str(corpus), str(review)],
        check=True,
        capture_output=True,
        text=True,
    )
    record = json.loads((review / "manifest.jsonl").read_text(encoding="utf-8"))
    graph = json.loads((review / "graphs" / "Control.json").read_text(encoding="utf-8"))
    summary = json.loads((review / "summary.json").read_text(encoding="utf-8"))
    assert record["status"] == "ok" and record["selected_solc"] == graph["graph"]["solc_version"]
    assert summary["ok"] == 1 and summary["error"] == 0
    assert summary["extractor_version"] == "0.3.0"
    assert summary["slither_version"] == "0.11.5"

    timed_review = tmp_path / "timed-review"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "spider.batch",
            str(corpus),
            str(timed_review),
            "--timeout",
            "0.001",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    timed_record = json.loads((timed_review / "manifest.jsonl").read_text(encoding="utf-8"))
    assert timed_record["status"] == "error" and timed_record["error"].startswith("TimeoutExpired:")
