# Graph schema

Spider emits directed NetworkX node-link JSON with format identifier
`spider-cpg/1.0`.

## Document shape

```json
{
  "directed": true,
  "multigraph": true,
  "graph": {},
  "nodes": [],
  "links": []
}
```

`nodes` contains typed program entities and operations. `links` contains typed
directed relations. Parallel relations between the same pair of nodes are
allowed.

## Graph metadata

| Field | Meaning |
| --- | --- |
| `format` | Graph contract identifier, currently `spider-cpg/1.0`. |
| `tool` | Extractor name, `Spider`. |
| `source` | Resolved absolute path of the input file or project directory. |
| `input_kind` | `file` or `directory`. |
| `input_sources` | Sorted canonical paths compiled directly from the supplied input. |
| `scope` | `file` for a single-source file extraction or `project` for imported/multi-source and directory extraction. |
| `node_type_key`, `edge_type_key` | Field containing the semantic type of each node or edge; both are `label`. |
| `position_key` | Field containing canonical node position, `order`. |
| `schema` | Human-readable graph schema name. |
| `attribute_schema` | Finite semantic-attribute contract, currently `spider-attributes/1`. |
| `source_anchor_schema` | Source-anchor contract, currently `spider-source-anchor/1`. |
| `source_anchor_unit`, `source_hash` | Byte-range unit and source hashing convention. |
| `extractor_version` | Installed `spider-solidity-cpg` package version. |
| `slither_version`, `solc_select_version` | Installed dependency versions. |
| `solc_version`, `solc_args` | Selected compiler release and arguments. |
| `compiler_semantic_regime` | Whether the selected release uses checked arithmetic by default. |
| `node_types`, `edge_types` | Sorted labels present in this graph. |
| `source_files` | Ordered manifest of compiler-resolved source units. |
| `source_mapping_coverage` | Counts of `present`, `missing`, and `synthetic` node mappings. |
| `node_ordering` | Canonicalization algorithm identifier, currently `spider-canonical-v1`. |
| `has_inline_assembly` | Whether a contract-level node reports inline assembly. |
| `unsupported_semantics` | Explicit list of encountered semantics that are not expanded. |
| `learnable_attribute_keys` | Finite feature fields approved by the attribute schema. |

Consumers should derive a corpus vocabulary from the union of `node_types` and
`edge_types` across its graphs.

## Node fields

Every node contains the following fields. A source position may be `null` when
Slither does not provide an exact mapping or when Spider creates a synthetic
node.

| Field | Meaning |
| --- | --- |
| `id` | Canonical label-prefixed identifier unique within the graph. |
| `label` | Semantic node type. |
| `name` | Declaration name or operation representation. |
| `order` | Unique contiguous canonical rank. |
| `file` | Canonical absolute source path, or `null`. |
| `file_id` | Index into `graph.source_files`, or `null`. |
| `line_start`, `line_end` | Compiler-reported inclusive line range, or `null`. |
| `column_start`, `column_end` | Compiler-reported columns, or `null`. |
| `byte_start`, `byte_end` | Half-open UTF-8 byte range, or `null`. |
| `code` | Trimmed source text decoded from the byte range, or an empty string. |
| `source_mapping_status` | `present`, `missing`, or `synthetic`. |
| `anchor_origin` | `exact`, `cfg_fallback`, `missing`, or `synthetic`. |

Node types may add finite semantic attributes such as declaration role,
visibility, mutability, operator family, data location, or type family.

## Edge fields

Every edge contains `source`, `target`, and `label`. Relation-specific fields
may include `argument_index`, `return_index`, `operand_index`,
`modifier_index`, `callsite`, `variable`, `guard`, `via`, `branch`, and
`transitive`.

Representative relation groups are:

| Group | Labels |
| --- | --- |
| Structure | `AST`, `CONTAINS`, `OPERAND`, `REF` |
| Control | `CFG`, `EVAL_ORDER`, `TRUE_BRANCH`, `FALSE_BRANCH`, `TRY_SUCCESS`, `TRY_FAILURE`, `DOMINATE`, `POST_DOMINATE`, `CDG` |
| Data | `READS`, `WRITES`, `STATE_READ`, `STATE_WRITE`, `REACHING_DEF` |
| Access | `INDEX_BASE`, `INDEX_KEY`, `MEMBER_BASE`, `MEMBER_FIELD` |
| Calls | `INTERNAL_CALL`, `EXTERNAL_CALL`, `LOW_LEVEL_CALL`, `DELEGATECALL`, `ETHER_SEND`, `ETHER_TRANSFER` |
| Interprocedural | `VALUE_TO_ARGUMENT`, `ARGUMENT_TO_PARAMETER`, `RETURN_VALUE`, `RETURN_TO_CALLER`, `XCFG_CALL`, `XCFG_RETURN` |
| Effects | `CALL_READS_STATE`, `CALL_WRITES_STATE` |

The complete vocabulary for a graph is stored in its metadata.

## Source manifest and anchors

`graph.source_files` is ordered by canonical path. Every entry contains:

- `file_id`
- canonical `path`
- SHA-256 of the raw source bytes
- byte length
- `utf-8` encoding

The tuple `(file_id, byte_start, byte_end)` is the source lookup key. Byte
ranges are half-open and refer to the raw UTF-8 source bytes recorded by the
manifest. `code` is derived from that range when the mapping is present.

## Access provenance

Every `INDEX_ACCESS` has exactly one `INDEX_BASE` and one `INDEX_KEY`. Every
`MEMBER_ACCESS` has exactly one `MEMBER_BASE` and one `MEMBER_FIELD`; the field
target is a `MEMBER_NAME` child.

Nested accesses remain compositional. In
`records[msg.sender].blockNumber`, the member base targets the index operation,
whose base and key target `records` and `msg.sender`.

## Calls and returns

- `ARGUMENT` links a call operation to its indexed argument wrapper.
- `VALUE_TO_ARGUMENT` links each proven producer operation to that wrapper.
- `ARGUMENT_TO_PARAMETER` binds a source-resolved argument position to a formal
  parameter.
- `RETURN_VALUE` binds a return operation to a formal return position.
- `RETURN_TO_CALLER` binds that formal return position to the callsite.
- `XCFG_CALL` enters an implemented source target.
- `XCFG_RETURN` connects the target exit to the callsite's evaluation
  continuation.

`ARGUMENT` and `VALUE_TO_ARGUMENT` can describe an unresolved callsite.
Target-dependent bindings (`ARGUMENT_TO_PARAMETER`, `RETURN_TO_CALLER`,
`XCFG_CALL`, and `XCFG_RETURN`) are emitted only when the compiler resolves a
source target. Runtime addresses and unresolved dynamic targets do not receive
guessed target bindings.

`CALL_READS_STATE` and `CALL_WRITES_STATE` summarize state effects. The
`transitive` field is `false` for a direct callee effect and `true` when the
effect is inherited through a nested resolved call or modifier.

## Semantic attributes

`graph.attribute_schema` is `spider-attributes/1`.
`graph.learnable_attribute_keys` lists finite compiler-derived features that
can be used without treating identifiers or source text as categorical
vocabulary.

Raw names, code, literal values, IDs, canonical order, and vulnerability labels
are not included in the learnable-attribute whitelist.

## Compatibility

Consumers should check `graph.format` before loading a graph and record
`extractor_version` with derived datasets. Additive node or edge labels may
appear while the format remains `spider-cpg/1.0`; consumers must therefore use
the vocabularies stored in each graph rather than a hard-coded label list.

A change to required fields, field meanings, anchor units, or relation
semantics requires a new graph format identifier. Graphs produced by different
extractor versions should not be mixed in one model artifact without rebuilding
its vocabulary, canonical hashes, and experiment signature.

## Vulnerability subgraph document

`--vulnerability` adds a separate NetworkX node-link document with format
`spider-vulnerability-subgraph/1.0`; it does not change the required full CPG
contract. Its `graph` metadata contains:

| Field | Meaning |
| --- | --- |
| `parent_format`, `parent_sha256` | Format and exact serialized SHA-256 of the validated parent CPG. |
| `selection` | Requested canonical class or `all`. |
| `taxonomy` | Ordered eight-class taxonomy understood by this Spider version. |
| `detector`, `model_schema` | `rules` or `hybrid`, plus the optional model contract. |
| `max_hops` | Retrieval closure radius around every finding seed. |
| `max_nodes` | Requested per-finding node cap for bounded retrieval. All valid seeds are retained; a finding with more seeds than this value may exceed the cap to preserve seed provenance. |
| `findings` | Ordered class, score, detector, rule, evidence, seed IDs, and selected node IDs. Model-only findings may also include `model_anchor_node_ids` and `model_threshold`. |
| `node_types`, `edge_types` | Sorted vocabularies present in the unioned subgraph. |

Subgraph nodes preserve their original IDs, canonical order, source anchors,
and semantic attributes. They add `vulnerability_classes`,
`vulnerability_findings`, and `vulnerability_seed`. The subset is intentionally
not recanonicalized: parent IDs are the provenance link. Links preserve their
parent attributes; a `callsite` field is omitted when its referenced node is
outside the selected union.

The optional model format is `spider-vulnerability-model/1.0`. It contains the
exact ordered taxonomy and one logistic classifier per class with `intercept`,
recall-oriented `threshold`, and a finite map of feature weights. Loading fails
when either schema or taxonomy differs. Runtime model-only findings additionally
require class-specific local structural anchors; graph-level score alone is not
treated as line-level evidence.

The optional GNN hand-off format is
`spider-vulnerability-localizer/1.0`. It is a parent-bound candidate document,
not an exploitability proof. Its required fields are `parent_format`,
`parent_sha256`, the exact eight-class `classes` list, `score_space` set to
`probability`, and `findings`. Each finding contains `class`, `global_score`,
`local_score`, `threshold`, `provenance`, `node_scores`, and
`seed_node_ids`. Spider rejects a parent-hash mismatch, non-finite scores,
unknown provenance, missing node scores, duplicate seeds, and taxonomy drift.
Only source-anchored scored nodes become seeds; the existing typed closure and
per-finding node budget then build the final JSON/DOT report. The accepted
provenances are `trained_node_head`, `weak_model`, and `rule`.
