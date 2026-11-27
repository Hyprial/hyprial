# OrgFS sync wire contract (protocol 3)

OrgFS nodes exchange the current state of a space, not a log. This contract
covers the four exchanges between nodes. `<prefix>` is the transport key
prefix. Design: `docs/design/orgfs-minimal-design-2026-11-25.md` §4.

There is no compatibility with the earlier journal protocol: its keys have no
subscriber and no supplier, and an announce without `"protocol": 3` is ignored.

| Exchange | Key | Kind | `schemaVersion` |
| --- | --- | --- | --- |
| Pull | `<prefix>/org/fs3/sync/<spaceId>/<node>` | query | 3 |
| Broadcast | `<prefix>/org/fs3/update/<spaceId>` | put / subscribe | 3 |
| Announce | `<prefix>/org/fs/announce/<node>` | put / subscribe | 1 |
| Blob | `<prefix>/org/fs/blob/<digest>` | query | 1 |

Pull and broadcast frames are JSON objects. A decoder validates the whole
frame before anything is applied, and refuses unknown top-level keys: the
version field is the extension point.

## Pull

Request, sent to one supplier node:

```json
{"schemaVersion": 3, "type": "sync", "spaceId": "<space>", "round": "<uuid>",
 "requester": {"node": "<node>", "author": "<principal>"},
 "vectors": {"meta": "<b64 state vector>", "tree": "<b64 state vector>",
             "doc-…": "<b64 state vector>"},
 "maxBytes": 1048576}
```

`vectors` always holds `meta` and `tree`, plus at most 128 content documents;
a document the requester does not hold carries an empty vector.

Reply:

```json
{"schemaVersion": 3, "type": "sync-reply", "spaceId": "<space>", "round": "<uuid>",
 "supplier": "<node>",
 "updates": [{"docId": "meta", "update": "<b64>", "vector": "<b64 state vector>"},
             {"docId": "doc-…", "blob": "<sha256>", "size": 123456,
              "vector": "<b64 state vector>"}],
 "vectors": {"meta": "<b64 state vector>", "tree": "<b64 state vector>"},
 "omitted": ["doc-…"]}
```

- For each requested document the supplier returns the update the requester
  lacks together with the supplier's current vector of that document. A
  document the requester already covers is not returned.
- An update larger than half the page budget is not inlined. The reply names
  a temporary blob (`blob`, `size`) that the requester fetches over the blob
  exchange and checks against the digest.
- Documents that did not fit are listed in `omitted`; the requester asks again.
- `vectors` are the supplier's `meta` and `tree` vectors. One round does not
  carry updates in both directions. A supplier that sees request vectors it
  does not cover starts its own pull from the requester.

Refusal:

```json
{"schemaVersion": 3, "type": "sync-error", "spaceId": "<space>", "round": "<uuid>",
 "supplier": "<node>", "code": "<code>", "message": "<text>"}
```

`round` is the empty string when the request could not be parsed.

Who is served:

| `requester.author` | Served |
| --- | --- |
| Current member of the space | Every requested document |
| Removed member (its member row remains, marked removed) | `meta` only, so it learns of its removal |
| Never a member | Nothing; `not-a-member` |

`requester.node` and `requester.author` are self-reported. These rules bind
unmodified nodes; they are not cryptographic isolation.

## Broadcast

After a local commit is durable, the node publishes one frame per changed
document:

```json
{"schemaVersion": 3, "type": "update", "spaceId": "<space>", "node": "<node>",
 "docId": "<doc>", "update": "<b64>", "vector": "<b64 state vector after the commit>"}
```

A frame that would exceed 2 MiB is sent without `update`; the other fields
stay. A receiver that gets such a frame, or whose admission answers that a
predecessor is missing, pulls from `node`.

A receiver applies a broadcast only when the sending node belongs to a current
member. A receiver that is itself a removed member applies `meta` broadcasts
only. Broadcasts are not acknowledged and can be lost; pulls repair the loss.

## Announce

```json
{"schemaVersion": 1, "type": "orgfs-announce", "protocol": 3, "node": "<node>",
 "durable": true,
 "spaces": [{"spaceId": "<space>", "roles": ["member"], "mode": "rw"}]}
```

When the frame would exceed 2 MiB, each entry of `spaces` carries `spaceId`
only.

## Blob

Request for one chunk of a binary content or of a temporary sync blob:

```json
{"schemaVersion": 1, "type": "orgfs-blob", "spaceId": "<space>", "digest": "<sha256>",
 "requester": {"node": "<node>", "author": "<principal>"},
 "offset": 0, "length": 768000}
```

`requester` is required. `offset` defaults to 0 and must be a multiple of
768000; `length` defaults to 768000 and may not exceed it.

Reply:

```json
{"schemaVersion": 1, "type": "orgfs-blob-chunk", "digest": "<sha256>",
 "offset": 0, "length": 768000, "total": 123456789, "data": "<b64>"}
```

A supplier answers only a requester whose author is a current member of
`spaceId`; any other requester gets an `orgfs-error` frame with code
`not-a-member`. The requester verifies the digest of the assembled bytes.
