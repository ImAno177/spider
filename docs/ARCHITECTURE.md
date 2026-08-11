# Architecture

Spider is intentionally a small compiler-backed pipeline rather than a query
server or framework.

```text
Solidity entry file
  -> pragma parsing and compiler fingerprinting
  -> solc + Slither project compilation
  -> declarations and source anchors
  -> CFG blocks and ordered SlithIR operations
  -> reaching definitions and control dependence
  -> source-resolved calls, modifiers, returns, and state effects
  -> canonical node IDs/order
  -> structural verifier
  -> NetworkX node-link JSON and optional DOT
```

## Modules

| Module | Responsibility |
| --- | --- |
| `spider/solc.py` | Parse pragmas, fingerprint installed compilers, and choose deterministic compatible candidates. |
| `spider/extract.py` | Compile the project and construct the typed graph. |
| `spider/verify.py` | Recompute and validate structural, control-flow, data-flow, call, and modifier invariants. |
| `spider/__main__.py` | Single-entry extraction CLI and repeated multi-view exports. |
| `spider/batch.py` | Isolated corpus extraction with JSONL provenance and summary output. |

## Compiler selection

Spider reads the entry file pragma, enumerates installed release compilers, and
tries compatible candidates deterministically. Each executable is checked by
SHA-256 and `--version`; mislabeled or prerelease binaries are rejected.

For pre-1.0 Solidity, Spider tries the earliest compatible minor family first
and the newest patch within that family. Failed candidates remain visible until
one successfully constructs the Slither project.

## Graph construction

Declarations are registered before function bodies so imported and inherited
references can resolve by canonical source identity. Each Slither CFG node owns
ordered SlithIR operation nodes. Empty blocks forward evaluation to their CFG
successors.

Reaching definitions are computed as a CFG fixpoint. Temporary and reference
producers are function-scoped; declared locals and state variables use the
reaching definitions proven at each consumer.

## Interprocedural overlay

Only compiler-resolved source targets receive semantic call edges. Spider adds
argument/parameter binding, return/caller binding, XCFG entry/continuation
edges, and a finite state-effect fixpoint through calls and modifiers.

Unresolved calls retain a typed `CALL_TARGET` but do not receive a guessed
source implementation.

## Determinism

Before serialization, Spider applies an attributed neighborhood refinement to
canonicalize node order and IDs independently of Slither collection order.
Edges are sorted by their complete serialized payload.

## Verification

The verifier does not trust extractor metadata. It recomputes vocabularies,
control dependence, evaluation order, call/return structure, modifier overlays,
state effects, access provenance, and source-anchor constraints. An invalid
graph causes the extraction CLI to exit non-zero before writing output.
