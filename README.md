<p align="center">
  <img src="logo.png" alt="Spider logo" width="96" height="96">
</p>

<h1 align="center">Spider</h1>

<p align="center">
  A compiler-backed Solidity code property graph extractor and verifier.
</p>

Spider compiles Solidity with a pragma-compatible `solc`, derives semantic
operations from Slither/SlithIR, and emits a deterministic typed CPG as
NetworkX node-link JSON. It models control flow, data dependence, modifiers,
calls, indexed/member storage access, and source-resolved cross-contract flow.

The current graph format is `spider-cpg/1.0`.

## Highlights

- Compiler-backed extraction across historical and current Solidity releases.
- Exact UTF-8 byte spans and SHA-256 manifests for every resolved source unit.
- Typed AST, CFG, evaluation order, dominance, control dependence, and reaching
  definitions.
- Solidity-specific state reads/writes, modifiers, Ether transfers, low-level
  calls, return checks, and inline-assembly coverage flags.
- Exact index base/key and member base/field provenance.
- Source-resolved argument, return, XCFG, and transitive state-effect flow
  across contracts and imports.
- Deterministic canonical node IDs and strict structural verification.
- Focused Graphviz exports for AST, CFG, CDG, DDG, PDG, calls, or the full CPG.

## Installation

Spider requires Python 3.10 or newer and installed Solidity compiler releases.

```bash
git clone https://github.com/ImAno177/spider.git
cd spider
python -m pip install -e '.[dev]'

solc-select install 0.4.25
solc-select install 0.8.11
```

Install every compiler family needed by your corpus. Spider fingerprints each
binary, rejects prerelease or mislabeled executables, and records the compiler
and package versions in graph metadata.

## Quick start

Extract and verify one contract:

```bash
spider Contract.sol out/contract.json
spider-verify out/contract.json
```

Render a focused interprocedural call view:

```bash
spider Contract.sol out/contract.json \
  --export calls=out/contract-calls.dot \
  --export pdg=out/contract-pdg.dot

dot -Tpng out/contract-calls.dot -o out/contract-calls.png
```

Compile projects with import remappings:

```bash
spider contracts/Vault.sol out/vault.json \
  --solc-remap '@openzeppelin/=vendor/openzeppelin/' \
  --solc-remap '@chainlink/=vendor/chainlink/'
```

Use `--solc-version VERSION` to require one installed release instead of
automatic pragma selection.

## CLI

```text
spider SOURCE OUTPUT
       [--export MODE=PATH]
       [--solc-remap REMAPPING]
       [--solc-version VERSION]

spider-verify GRAPH
spider-batch DATASET OUTPUT [--timeout SECONDS] [--solc-remap REMAPPING]
```

`--export` may be repeated. A single compilation can emit any combination of
`ast`, `cfg`, `cdg`, `ddg`, `pdg`, `calls`, and `cpg` DOT views. Use
`edge:LABEL=PATH` for a view containing one exact edge type.

## Python API

```python
from spider import extract
from spider.verify import validate

graph = extract(
    "contracts/Vault.sol",
    solc_remaps=["@openzeppelin/=vendor/openzeppelin/"],
)

errors = validate(graph)
if errors:
    raise RuntimeError("\n".join(errors))
```

## Graph model

| Area | Representative labels |
| --- | --- |
| Declarations | `CONTRACT`, `FUNCTION`, `SOLIDITY_MODIFIER`, `STATE_VARIABLE`, `PARAMETER`, `RETURN_PARAMETER` |
| Operations | `ASSIGNMENT`, `INDEX_ACCESS`, `MEMBER_ACCESS`, `MEMBER_NAME`, `CALL`, `RETURN` |
| Control | `AST`, `CONTAINS`, `CFG`, `EVAL_ORDER`, `DOMINATE`, `POST_DOMINATE`, `CDG` |
| Data | `READS`, `WRITES`, `STATE_READ`, `STATE_WRITE`, `REACHING_DEF` |
| Access provenance | `INDEX_BASE`, `INDEX_KEY`, `MEMBER_BASE`, `MEMBER_FIELD` |
| Calls | `INTERNAL_CALL`, `EXTERNAL_CALL`, `LOW_LEVEL_CALL`, `DELEGATECALL`, `ETHER_SEND`, `ETHER_TRANSFER` |
| Interprocedural | `VALUE_TO_ARGUMENT`, `ARGUMENT_TO_PARAMETER`, `RETURN_VALUE`, `RETURN_TO_CALLER`, `XCFG_CALL`, `XCFG_RETURN` |
| Effect summaries | `CALL_READS_STATE`, `CALL_WRITES_STATE` with a `transitive` flag |

Each node has a stable `label`, contiguous canonical `order`, source status,
and exact byte anchors when supplied by the compiler. `graph.node_types` and
`graph.edge_types` are the exact vocabularies present in that graph.

See [docs/SCHEMA.md](docs/SCHEMA.md) for the graph contract and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the extraction pipeline.

## Cross-contract semantics

Spider emits interprocedural facts only when the compiler resolves the source
target. A resolved call may carry:

```text
producer -> argument -> parameter
return operation -> return parameter -> caller
caller -> callee entry -> ... -> callee exit -> caller continuation
caller -> directly/transitively read or written state
```

State effects are computed to a deterministic fixpoint through source-resolved
calls and modifiers. Spider deliberately does not guess callback, proxy,
runtime-address, or unresolved `delegatecall` targets.

## Batch extraction

`spider-batch` recursively extracts `.sol` files into an empty output
directory. It records one JSONL manifest plus a corpus summary containing
success/failure status, compiler selection, runtime, and union vocabularies.

```bash
spider-batch dataset/ out/corpus --timeout 30
```

Each file runs in a fresh subprocess so compiler or Slither failures remain
isolated and visible.

## Verification

Run the repository gates from the project root:

```bash
python -m pytest -q
python -m ruff check .
python -m compileall -q spider tests scripts
python -m pip wheel . --no-deps --wheel-dir out/wheels
```

The regression suite includes mutation checks for source anchors, CFG and
evaluation order, modifier overlays, calls and returns, storage access,
argument producers, state effects, and inline-assembly coverage.

## Spider and Joern

Joern provides a broader query and analysis platform: CPGQL, overlays,
configurable taint semantics, server mode, plugins, and multiple exporters.
Its official frontend list does not currently include Solidity. Spider focuses
on compiler-derived Solidity semantics and emits a verified portable graph that
can feed custom analysis or graph-learning pipelines.

## Limitations

- Inline assembly/Yul is flagged but its internal operations are opaque.
- Analysis is not path-sensitive or symbolic execution.
- Storage-slot aliasing is not modeled.
- Modifier and state-effect overlays are context-insensitive.
- Runtime callbacks, proxy implementations, and dynamic targets are not
  inferred without compiler evidence.
- Compiler or SlithIR construction failures remain explicit extraction errors.

## Project documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Graph schema](docs/SCHEMA.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Roadmap](ROADMAP.md)

## License

No project license has been selected yet. Until a license is added, the source
is publicly viewable but no additional permissions are granted.
