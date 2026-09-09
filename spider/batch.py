import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from . import __version__
from .solc import pragma_from


def main() -> None:
    parser = argparse.ArgumentParser(prog="spider-batch", description="Batch-extract a Solidity corpus and write a validated manifest.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("dataset", type=Path, nargs="?")
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--project-manifest", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--solc-version", help="Require one installed solc version for every source; default selects from each pragma")
    parser.add_argument("--solc-remap", action="append", help="Solidity import remapping; repeat for multiple remappings")
    parser.add_argument("--timeout", type=float, help="Optional per-file extraction timeout in seconds")
    args = parser.parse_args()
    if args.project_manifest:
        if args.dataset is None or args.output is not None or args.solc_version or args.solc_remap:
            parser.error("project manifest cannot be combined with dataset/compiler overrides")
        from .project_batch import run_manifest

        summary = run_manifest(args.project_manifest, args.dataset, args.workers, args.timeout if args.timeout is not None else 600, args.resume)
        print(json.dumps(summary, indent=2))
        return
    if args.dataset is None or args.output is None or args.resume:
        parser.error("dataset is required; --resume requires --project-manifest")
    dataset = args.dataset.resolve()
    output = args.output.resolve()
    files = sorted(dataset.rglob("*.sol"))
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    records = []
    node_types: set[str] = set()
    edge_types: set[str] = set()
    tool_metadata: dict[str, str] = {}

    for source in files:
        started = time.monotonic()
        record = {
            "source": source.relative_to(dataset).as_posix(),
            "pragma": pragma_from(source),
            "requested_solc": args.solc_version or "auto",
        }
        try:
            destination = output / "graphs" / source.relative_to(dataset).with_suffix(".json")
            command = [sys.executable, "-m", "spider", str(source), str(destination)]
            if args.solc_version:
                command += ["--solc-version", args.solc_version]
            for remapping in args.solc_remap or []:
                command += ["--solc-remap", remapping]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip())
            graph = json.loads(destination.read_text(encoding="utf-8"))
            node_types.update(graph["graph"]["node_types"])
            edge_types.update(graph["graph"]["edge_types"])
            tool_metadata = {
                key: graph["graph"][key]
                for key in ("format", "extractor_version", "slither_version", "solc_select_version")
            }
            record.update(
                status="ok",
                selected_solc=graph["graph"]["solc_version"],
                solc_args=graph["graph"]["solc_args"],
                nodes=len(graph["nodes"]),
                edges=len(graph["links"]),
            )
        except (Exception, SystemExit) as error:
            record.update(status="error", selected_solc=None, error=f"{type(error).__name__}: {error}")
        record["seconds"] = round(time.monotonic() - started, 3)
        records.append(record)
        print(f"{record['status']:5} {record['source']}", flush=True)

    manifest = output / "manifest.jsonl"
    manifest.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    compilers = Counter(record["selected_solc"] for record in records if record["status"] == "ok")
    summary = {
        "files": len(records),
        "ok": sum(record["status"] == "ok" for record in records),
        "error": sum(record["status"] == "error" for record in records),
        "compilers": dict(sorted(compilers.items())),
        "node_types": sorted(node_types),
        "edge_types": sorted(edge_types),
        **tool_metadata,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
