# Network link-close observability

The daemon's additive `ps --json` diagnostics distinguish map-peer remaps from
ordinary Zenoh/TCP link churn.

`ps.forwarding.counters.map_peer_remap` is a non-negative, since-start
counter. It advances once when a known peer's sidecar `map_peer` endpoint
changes ports. A TCP connection's local ephemeral port changing does not
advance this counter.

`ps.zenoh.session.counters.zenoh_link_delete` is a non-negative,
since-start counter. It advances once for every Zenoh link `DELETE` event,
including an event that cannot be attributed to a directory peer.

The structured `zenoh.link.event` event keeps `kind`, `zid`, `src`, and `dst`.
When `kind` is `delete`, it also contains:

- `reason`: the reason supplied by Zenoh when available,
  `session_close:<local-cause>` for an inferred locally initiated session
  close, or `native-cause-unavailable` when neither source identifies it;
- `ageMs`: the non-negative link age from the corresponding `PUT`, or null if
  no matching creation event was observed;
- `peerNode`: the directory peer whose current map endpoint matches the link,
  or null;
- `localEphemeralPort`: the TCP client-side ephemeral port when it can be
  distinguished from a known configured/map endpoint, or null; and
- `peerMapPort`: the sidecar `map_peer` port when a peer match exists, or null;
- `zenohLinkDeleteCount`: the post-event since-start delete count; and
- `mapPeerRemapCount`: the current since-start sidecar remap count.

All fields are observational. Missing attribution is represented by null and
must not be guessed. Diagnostic delivery remains bounded and non-blocking for
the Zenoh callback thread.
