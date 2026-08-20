import argparse
import json
from pathlib import Path

from . import __version__
from .extract import DOT_REPRESENTATIONS, extract, to_dot
from .verify import validate
from .vulnerability import (
    VULNERABILITY_TYPES,
    detect_vulnerabilities,
    load_localizer,
    load_model,
    vulnerability_to_dot,
)


def _export_spec(value: str) -> tuple[str, Path]:
    mode, separator, destination = value.partition("=")
    if not separator or not mode or not destination:
        raise argparse.ArgumentTypeError("export must be MODE=PATH")
    if mode not in DOT_REPRESENTATIONS and not mode.startswith("edge:"):
        choices = ", ".join(sorted(DOT_REPRESENTATIONS))
        raise argparse.ArgumentTypeError(f"export mode must be one of {choices}, or edge:LABEL")
    if mode.startswith("edge:") and not mode.removeprefix("edge:"):
        raise argparse.ArgumentTypeError("edge export requires a label")
    return mode, Path(destination)


def main() -> None:
    parser = argparse.ArgumentParser(prog="spider", description="Extract a compiler-backed Solidity code property graph.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("source", type=Path, help="Solidity entry file or plain project directory")
    parser.add_argument("output", type=Path, help="NetworkX node-link JSON output")
    parser.add_argument("--export", action="append", type=_export_spec, metavar="MODE=PATH", help="Export a DOT view; repeat for multiple modes or use edge:LABEL=PATH")
    parser.add_argument("--solc-remap", action="append", help="Solidity import remapping; repeat when a project omits vendored dependencies")
    parser.add_argument("--solc-version", help="Require this installed solc version instead of selecting from the source pragma")
    parser.add_argument("--vulnerability", choices=("all", *VULNERABILITY_TYPES), help="Detect one vulnerability class or all eight classes and export one combined JSON/DOT subgraph pair")
    parser.add_argument("--vulnerability-output", type=Path, metavar="PREFIX", help="Vulnerability output prefix; defaults to OUTPUT.<CLASS>")
    parser.add_argument("--vulnerability-model", type=Path, metavar="MODEL.json", help="Optional Spider linear model; rule detection remains active")
    parser.add_argument("--vulnerability-localizer", type=Path, metavar="LOCALIZER.json", help="Optional GNN node-localizer candidate document")
    parser.add_argument("--vulnerability-hops", type=int, default=2, metavar="N", help="CPG closure hops around vulnerability seeds (default: 2)")
    parser.add_argument("--vulnerability-max-nodes", type=int, default=96, metavar="N", help="Maximum retrieval nodes per finding (default: 96)")
    args = parser.parse_args()

    if not args.vulnerability and (args.vulnerability_output or args.vulnerability_model or args.vulnerability_localizer):
        parser.error("vulnerability output, model, and localizer options require --vulnerability")
    if args.vulnerability_hops < 0:
        parser.error("--vulnerability-hops must be non-negative")
    if args.vulnerability_max_nodes <= 0:
        parser.error("--vulnerability-max-nodes must be positive")

    graph = extract(args.source, args.solc_remap, solc_version=args.solc_version)
    errors = validate(graph)
    if errors:
        raise SystemExit("Invalid extracted graph:\n" + "\n".join(f"- {error}" for error in errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    destinations = [destination.resolve() for _, destination in args.export or []]
    if len(destinations) != len(set(destinations)):
        parser.error("each export must use a distinct path")
    for mode, destination in args.export or []:
        destination.parent.mkdir(parents=True, exist_ok=True)
        edge_labels = {mode.removeprefix("edge:")} if mode.startswith("edge:") else None
        representation = None if edge_labels else mode
        destination.write_text(to_dot(graph, edge_labels, representation), encoding="utf-8")
    if args.vulnerability:
        prefix = args.vulnerability_output or Path(f"{args.output.with_suffix('')}.{args.vulnerability}")
        if prefix.suffix in {".json", ".dot"}:
            prefix = prefix.with_suffix("")
        json_path = Path(f"{prefix}.json")
        dot_path = Path(f"{prefix}.dot")
        reserved = {args.output.resolve(), *destinations}
        if json_path.resolve() in reserved or dot_path.resolve() in reserved:
            parser.error("vulnerability outputs must not overwrite the full graph or a DOT export")
        model = load_model(args.vulnerability_model) if args.vulnerability_model else None
        localizer = load_localizer(args.vulnerability_localizer) if args.vulnerability_localizer else None
        report = detect_vulnerabilities(
            graph,
            args.vulnerability,
            model=model,
            localizer=localizer,
            max_hops=args.vulnerability_hops,
            max_nodes=args.vulnerability_max_nodes,
        )
        json_path.parent.mkdir(parents=True, exist_ok=True)
        dot_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        dot_path.write_text(vulnerability_to_dot(report), encoding="utf-8")
        print(f"Spider: {len(report['graph']['findings'])} suspicious findings -> {json_path}, {dot_path}")
    print(f"Spider: {len(graph['nodes'])} nodes, {len(graph['links'])} edges -> {args.output}")


if __name__ == "__main__":
    main()
