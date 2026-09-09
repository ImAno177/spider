"""Checkpoint the unchanged Spider release over a complete frozen inventory."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def run(inventory_path: Path, original_spider: Path, output: Path, workers: int = 2) -> dict:
    original_spider, output = original_spider.resolve(), output.resolve()
    if workers < 1:
        raise ValueError("workers must be positive")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    root = Path(inventory["source_root"])
    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONPATH=str(original_spider), PYTHONUTF8="1")
    code_signature = hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in sorted((original_spider / "spider").glob("*.py")))).hexdigest()

    def one(record):
        relative = record["path"]
        identifier = hashlib.sha256(relative.encode()).hexdigest()
        destination = output / identifier
        destination.mkdir(exist_ok=True)
        result_path = destination / "result.json"
        if result_path.exists():
            saved = json.loads(result_path.read_text())
            if saved.get("sha256") == record["sha256"] and saved.get("code_signature") == code_signature:
                return saved
        started = time.monotonic()
        result = dict(record, status="error", code_signature=code_signature)
        try:
            source = root / relative
            if hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError("inventory checksum mismatch")
            command = [sys.executable, "-m", "spider", str(source), str(destination / "graph.json")]
            with (destination / "stdout.log").open("wb") as stdout, (destination / "stderr.log").open("wb") as stderr:
                process = subprocess.Popen(command, cwd=original_spider, env=environment, stdout=stdout, stderr=stderr)
                try:
                    returncode = process.wait(timeout=600)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                    process.kill()
                    process.wait()
                    raise
            result.update(status="ok" if returncode == 0 else "error", returncode=returncode)
            if returncode:
                result["error"] = (destination / "stderr.log").read_text(encoding="utf-8", errors="replace")[-12000:]
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        result["seconds"] = time.monotonic() - started
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result), encoding="utf-8")
        temporary.replace(result_path)
        return result

    records = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, record) for record in inventory["files"] if record.get("kind", "solidity") == "solidity"]
        for future in as_completed(futures):
            records.append(future.result())
            if len(records) % 100 == 0:
                print(json.dumps({"processed": len(records), **Counter(r["status"] for r in records)}), flush=True)
    records.sort(key=lambda r: r["path"])
    (output / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    summary = {"files": len(records), **Counter(r["status"] for r in records), "inventory_sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest()}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("original_spider", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(run(args.inventory, args.original_spider, args.output, args.workers)))
