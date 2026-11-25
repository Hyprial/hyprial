# Decision notes (conversation plan slice C1; owner ruling D1/D2, 2026-11-17):
# - A message.send without a conversationId belongs to the default
#   conversation of its two ends.  The id is derived from one key per end,
#   so both nodes name the same conversation without coordination.
# - Two steps, both pure.  endpoint_key turns an address into the most stable
#   key its text alone gives; default_conversation_id only orders and hashes
#   two keys.  A caller that knows a better key for an end (a later slice may
#   resolve a user to its agent id) passes that key instead.
# - The pair is unordered: the two keys are sorted before they are framed, so
#   (a, b) and (b, a) give one id.  A self pair is valid.
# - Defined for ANY two ends, including pairs that have no thread (reserved
#   daemon actors, user to user, three-segment adapter addresses, route
#   targets, bare names).  Nothing is rejected and nothing raises.
# - Keys (coordinator adjustment, 2026-11-17: the id must survive a change of
#   an agent's address):
#   * id-form agent address agent:<owner>:<machine>:a.<id>: the a.<id> alone.
#     The id is a hash of the incarnation's 128-bit entity token (identity
#     agent_id_for_entity_token), so the same agent addressed under another
#     machine or owner segment keeps its default conversations.  The token is
#     random for every agent, the owner's user agent included (UA4: each
#     machine's instance is an independent agent).  The send path
#     canonicalises user agent addresses to user:<owner> before keys are
#     derived.
#   * name-form agent address (reserved daemon actors, names with no record,
#     frozen legacy names): the full canonical address.  Mapping a name to
#     its a.<id> needs the registry of the node that hosts the agent; the
#     other node cannot do it, so the two directions would disagree.  Such a
#     pair therefore has a different id than its id-form twin.  Sends to the
#     name of a registered agent are already refused as retired.
#   * user:<owner>: the canonical user address, for now.  The key of a user
#     is expected to become its agent id; that moves every default
#     conversation with a user end to a new id, and messages stored under the
#     old id stay under it unless a reader also looks the old id up.
#   * adapter, route and everything else: the address itself.  The retired
#     "channel:" spelling is no longer read as "adapter:" (F1c, 2026-11-24):
#     every adapter writes "adapter:", and an old "channel:" end keys as its
#     own text, so its default conversation id differs from the adapter:
#     twin.  A string no parser recognises is used exactly as given: no case
#     folding and no trimming, as for thread ids.
# - The value is "dc." plus 26 lowercase base32 characters (the first 16
#   bytes of a SHA-256 over a tagged, length-framed seed).  The prefix marks a
#   derived default; it is a convention, not a reservation: a caller may still
#   pass any string, including one of this shape, as an explicit id.
# - The formula differs on purpose from the GUI's
#   uuid5("hyprial:gui:conversation:{sender}:{target}"), which is ordered.
# - Pure: no clock, no I/O, no daemon state.  Changing the tag, the framing
#   or a key rule moves every default conversation; the unit tests pin
#   literal ids for that reason.
"""Pure default-conversation identity: endpoint keys and the ``dc.`` id."""

from __future__ import annotations

import base64
import hashlib

from hyprial.kernel import is_agent_id_segment, parse_agent_uri

DEFAULT_CONVERSATION_PREFIX = "dc."
DEFAULT_CONVERSATION_SEED_TAG = b"hyprial-default-conversation-v1\0"

__all__ = [
    "DEFAULT_CONVERSATION_PREFIX",
    "DEFAULT_CONVERSATION_SEED_TAG",
    "default_conversation_id",
    "default_conversation_seed",
    "endpoint_key",
]


def endpoint_key(address: str) -> str:
    """The most stable key the text of *address* gives for one end."""

    text = address if isinstance(address, str) else str(address)
    agent = parse_agent_uri(text)
    if agent is not None and is_agent_id_segment(agent[2]):
        return agent[2]
    return text


def default_conversation_seed(key_a: str, key_b: str) -> bytes:
    """The length-framed seed of the unordered pair of endpoint keys."""

    framed = bytearray(DEFAULT_CONVERSATION_SEED_TAG)
    for part in sorted((str(key_a), str(key_b))):
        encoded = part.encode("utf-8", "surrogatepass")
        framed += len(encoded).to_bytes(4, "big") + encoded
    return bytes(framed)


def default_conversation_id(key_a: str, key_b: str) -> str:
    """The default conversation of two endpoint keys, in either order."""

    digest = hashlib.sha256(default_conversation_seed(key_a, key_b)).digest()[:16]
    return DEFAULT_CONVERSATION_PREFIX + (
        base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    )
