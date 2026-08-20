# Repository instructions

These instructions apply to the entire Spider repository.

## Source of truth

| Path | Responsibility |
| --- | --- |
| `spider/extract.py` | Public extraction API, Slither compatibility, and project compilation. |
| `spider/_builder.py` | Build CPG semantics from a compiled Slither unit. |
| `spider/_graph.py` | Graph primitives, metadata, canonicalization, and DOT rendering. |
| `spider/schema.py` | Public graph format identifier shared by code and CI. |
| `spider/verify.py` | Recompute and validate graph invariants. |
| `spider/solc.py` | Select and fingerprint compatible Solidity compilers. |
| `spider/__main__.py` | `spider` extraction and multi-view export CLI. |
| `spider/batch.py` | Isolated corpus extraction. |
| `spider/vulnerability.py` | Explainable vulnerability rules, optional model scoring, subgraph union, and DOT. |
| `spider/train_vulnerability.py` | Optional eight-class model training and artifact reports. |
| `tests/test_extract.py` | Semantic and mutation regression gate. |
| `docs/SCHEMA.md` | Public graph contract. |

Keep extractor and verifier semantics synchronized. Executable code overrides
prose only after the schema documentation and tests are updated.

## Required checks

```bash
python -m pytest -q
python -m ruff check .
python -m compileall -q spider tests scripts
python -m pip wheel . --no-deps --wheel-dir out/wheels
```

Run a representative `spider-batch` corpus gate when changing compiler
handling, graph semantics, imports, or interprocedural behavior.

## Graph invariants

- Preserve deterministic, contiguous canonical node order and IDs.
- Keep `graph.node_types` and `graph.edge_types` equal to labels present.
- Resolve declarations and calls by canonical source identity, not Python object
  identity alone.
- Keep temporary producers function-scoped and declared-variable flow based on
  CFG reaching definitions.
- Preserve ordered SlithIR evaluation, including empty CFG blocks.
- Pair each source-resolved `XCFG_CALL` with `XCFG_RETURN` to the exact
  evaluation continuation.
- Bind arguments and returns only by matching source-resolved positions.
- Keep index base/key and member base/field relations exact and unique.
- Keep state summaries coherent with direct and transitive source-resolved
  dependencies.
- Never encode a guessed callback, proxy implementation, runtime address, or
  dynamic target as a fact.

## Code rules

- Target Python 3.10 or newer.
- Prefer standard-library data structures and indexed lookups on hot paths.
- Fix semantic bugs at shared extraction or validation points.
- Sort unordered Slither collections whenever they can affect output.
- Preserve source spans and Solidity-specific labels.
- Keep CLI failures visible and non-zero.
- Do not add frameworks, services, or abstractions without a demonstrated
  second consumer.

## Test rules

- Add the smallest fixture that reproduces a semantic bug.
- Add mutation coverage when the verifier should reject corrupted output.
- Use Solidity 0.4.25 for existing fixtures unless a behavior requires another
  compiler.
- Never weaken schema, source-anchor, CFG, call, modifier, or compiler checks to
  make a test pass.

## Repository hygiene

- Do not commit `out/`, build products, wheels, caches, virtual environments,
  corpus graphs, or generated review artifacts.
- Keep `README.md` and `docs/` aligned with public behavior.
- Do not select or change the project license without owner approval.
