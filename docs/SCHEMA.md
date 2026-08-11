# Graph schema

Spider emits directed NetworkX node-link JSON with format identifier
`spider-cpg/1.0`.

## Top-level shape

```json
{
  "directed": true,
  "multigraph": true,
  "graph": {},
  "nodes": [],
  "links": []
}
```

`graph.node_types` and `graph.edge_types` equal the sorted labels actually
present. Consumers should build corpus vocabularies by taking their union.

## Required node fields

| Field | Meaning |
| --- | --- |
| `id` | Canonical label-prefixed identifier within the graph. |
| `label` | Stable semantic node type. |
| `name` | Human-readable declaration or operation representation. |
| `order` | Unique contiguous canonical rank. |
| `file_id` | Index into `graph.source_files`, or `null`. |
| `byte_start`, `byte_end` | Half-open UTF-8 byte span, or `null`. |
| `source_mapping_status` | `present`, `missing`, or `synthetic`. |
| `anchor_origin` | `exact`, `cfg_fallback`, `missing`, or `synthetic`. |

## Required edge fields

Every edge contains `source`, `target`, and `label`. Relation-specific fields
include `argument_index`, `return_index`, `callsite`, `variable`, `guard`,
`branch`, and `transitive`.

## Source manifest

`graph.source_files` is ordered by canonical path. Every entry contains:

- `file_id`;
- canonical `path`;
- SHA-256 of raw source bytes;
- byte length; and
- `utf-8` encoding.

The pair `(file_id, byte_start, byte_end)` is the source lookup key.

## Access provenance

Every `INDEX_ACCESS` has exactly one `INDEX_BASE` and one `INDEX_KEY`. Every
`MEMBER_ACCESS` has exactly one `MEMBER_BASE` and one `MEMBER_FIELD`; the field
target is a `MEMBER_NAME` child.

Nested access remains compositional. For
`records[msg.sender].blockNumber`, the member base targets the index operation,
whose own base and key target `records` and `msg.sender`.

## Calls and returns

- `ARGUMENT` indexes a call's argument wrapper.
- `VALUE_TO_ARGUMENT` connects a proven producer operation to that wrapper.
- `ARGUMENT_TO_PARAMETER` binds a source-resolved argument position.
- `RETURN_VALUE` binds a return operation to a formal return position.
- `RETURN_TO_CALLER` binds that position back to the callsite.
- `XCFG_CALL` enters an implemented source target.
- `XCFG_RETURN` returns to the exact evaluation continuation.

`CALL_READS_STATE` and `CALL_WRITES_STATE` summarize state effects. Their
boolean `transitive` attribute is false for a direct callee effect and true when
the effect is inherited through a nested source-resolved call or modifier.

## Semantic attributes

`graph.attribute_schema` is `spider-attributes/1`. The
`graph.learnable_attribute_keys` whitelist contains finite compiler-derived
features such as declaration role, visibility, mutability, data location, type
family, operator family, literal category, and builtin role.

Raw names, code, values, IDs, orders, and vulnerability labels are not included
in that whitelist.

## Compatibility

The format identifier is stable across additive node/edge vocabulary changes.
Do not mix graphs built by different Spider versions in one model artifact
without rebuilding its vocabulary, canonical hashes, and experiment signature.
