# Architecture

Spider has five stages: compiler selection, project construction, graph
extraction, canonicalization, and validation.

```text
Solidity source file or project directory
  -> pragma parsing and compiler fingerprinting
  -> solc and Slither project construction
  -> declarations, source anchors, CFG blocks, and SlithIR operations
  -> control flow, data flow, modifiers, calls, returns, and state effects
  -> canonical node IDs and edge order
  -> graph validation
  -> NetworkX node-link JSON and optional DOT views
```

## Modules

| Module | Responsibility |
| --- | --- |
| `spider/solc.py` | Parse pragmas, fingerprint installed compilers, and choose compatible candidates in a deterministic order. |
| `spider/extract.py` | Apply Slither compatibility fixes, compile the input, and expose the public extraction API. |
| `spider/_builder.py` | Construct declarations, control/data flow, calls, returns, modifiers, and state-effect relations. |
| `spider/_graph.py` | Store graph elements, attach source metadata, canonicalize output, and render DOT views. |
| `spider/verify.py` | Recompute and validate source, structure, control-flow, data-flow, call, return, modifier, and state-effect invariants. |
| `spider/__main__.py` | Extract and validate one project, write JSON, and process repeated DOT exports. |
| `spider/batch.py` | Run isolated corpus extractions and write JSONL provenance and aggregate results. |

## Compiler selection

Spider reads the entry-source pragma or all non-empty pragmas in a project
directory, then inspects installed release compilers. Every candidate executable
is identified by SHA-256 and checked with `--version`; prerelease and mislabeled
binaries are rejected.

Compatible candidates are tried in deterministic order. For pre-1.0 Solidity,
Spider tries the earliest compatible minor family first and the newest patch
within that family. If no candidate constructs the Slither project, the final
candidate failure is raised.

## Project construction

For a file input, Slither compiles that entry source and resolves its imports.
For a directory input, Spider recursively discovers `.sol` files and submits
their original contents to one Solidity Standard JSON compilation. The selected
compiler must satisfy every discovered non-empty pragma. Remapping targets are
added to the same input when they resolve to local directories.

Directory extraction does not flatten source text. Flattening runs after a
project has already compiled and rewrites declarations into generated source
units, which would replace the original file identities and byte anchors. A
Standard JSON compilation provides one cross-contract Slither unit while
preserving the original source paths.

Version-control directories, build outputs, caches, virtual environments,
`node_modules`, and generated artifact directories are excluded from initial
directory discovery. Dependencies under those paths can be supplied through
`--solc-remap`.

Spider creates one source manifest entry for every input or resolved source
unit and records raw-byte SHA-256, byte length, encoding, and canonical path.
Directory inputs always have `project` scope. File inputs have `project` scope
when compilation resolves more than one source unit and `file` otherwise.

Declarations are registered before function bodies. This lets references from
imports and inheritance resolve through canonical source identity rather than
the order in which Slither returns objects.

## Intraprocedural extraction

Each Slither CFG block owns its ordered SlithIR operation nodes. `EVAL_ORDER`
preserves operation order within a block and forwards through empty CFG blocks.
Dominance, post-dominance, control dependence, reads, writes, and reaching
definitions are derived after the base CFG is present.

Temporary and reference producers remain function-scoped. Local and state
variable consumers use definitions that reach the consumer's CFG position.
Index and member accesses keep explicit base/key and base/field provenance.

## Interprocedural extraction

Calls receive semantic interprocedural relations only when their source target
is resolved. Spider binds argument positions to parameters, formal return
positions to callers, and callee entry/exit points to callsite continuations.

Direct state effects are collected from functions and modifiers. Transitive
effects are then computed to a deterministic fixpoint over resolved call and
modifier dependencies. Unresolved calls keep their typed call relations but do
not receive an inferred source implementation.

## Canonicalization

Before serialization, attributed neighborhood refinement assigns node order
and IDs independently of Slither collection order. Edges are sorted by their
complete serialized payload. Host-specific absolute paths are excluded from
the canonical node-order key; `file_id` and source anchors carry source
identity within the graph.

## Validation and output

The validator recomputes graph vocabularies and checks source anchors, control
and evaluation order, access provenance, call/return bindings, modifier
overlays, and state-effect summaries. The extraction CLI exits with a non-zero
status before writing output when validation fails.

The required output is one `spider-cpg/1.0` NetworkX node-link JSON document.
Repeated `--export` options can also write focused DOT views without recompiling
the project.
