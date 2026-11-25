"""Conversation records: write side (phase A, S6) and read projection (phase B).

After delivery, conversational bodies of both classes (interaction and,
since plan slice S1, system) are recorded under their conversation id in
the owner-home OrgFS space; once a record reads back, its body is granted
for release and the delivery queue may drop it after ack plus retention.
A conversation is not a cell (owner ruling 2026-11-16): ``conversation`` is
only the storage category naming the record directory.
``ConversationBodies`` is the delivery ``BodyResolver`` that hydrates a
dropped body from its record.  The package imports delivery
ports, cells and OrgFS, never the inbox engine or composition;
``composition.conversations`` wires it to the daemon.  The read projection
(``conversations.read``: heads, list cursors, the thread filter and the queue
threads) is pure and is what ``conversations.*`` consumes.
Since plan slice RM1 the record is the read model of ``conversations.*``:
``ConversationDocuments`` reads a conversation's record segments as one
document and ``conversations.merge`` folds it with the queue rows the
record lacks.
"""

from hyprial.daemon.impl.conversations.bodies import ConversationBodies
from hyprial.daemon.impl.conversations.document import ConversationDocuments
from hyprial.daemon.impl.conversations.keys import (
    DIRECT_KINDS,
    DIRECT_SEED_TAG,
    DirectEndpoint,
    DirectKey,
    DirectKind,
    direct_endpoint,
    direct_key,
    direct_seed,
)
from hyprial.daemon.impl.conversations.merge import (
    RecordMessage,
    merge_head_sets,
    merged_read,
    record_heads,
    visible_messages,
)
from hyprial.daemon.impl.conversations.read import (
    ConversationHead,
    QueueThread,
    conversation_heads,
    decode_list_cursor,
    encode_list_cursor,
    merge_heads,
    merge_queue_threads,
    page_heads,
    queue_threads,
    row_conversation,
    row_thread,
    thread_filtered,
)
from hyprial.daemon.impl.conversations.record import (
    CONVERSATION_KIND,
    RECORDER_THREAD,
    RECORDS_DATABASE,
    RECORD_FORMAT,
    ConversationRecorder,
    RecordObserver,
    InteractionFeed,
    RecordDraft,
    RecordEntry,
    RecordPass,
    RecordRef,
    RecordStore,
    RecordUnavailable,
    Redaction,
    RetentionFor,
    SegmentStore,
    ThreadIdFor,
    conversation_attribution,
    conversation_cell_id,
    conversation_key,
    conversation_pair_key,
    entry_key,
    no_redaction,
    retry_delay_s,
)

__all__ = [
    "CONVERSATION_KIND",
    "DIRECT_KINDS",
    "DIRECT_SEED_TAG",
    "RECORDER_THREAD",
    "RECORDS_DATABASE",
    "RECORD_FORMAT",
    "ConversationBodies",
    "ConversationDocuments",
    "ConversationHead",
    "ConversationRecorder",
    "RecordObserver",
    "QueueThread",
    "DirectEndpoint",
    "DirectKey",
    "DirectKind",
    "InteractionFeed",
    "RecordDraft",
    "RecordEntry",
    "RecordMessage",
    "RecordPass",
    "RecordRef",
    "RecordStore",
    "RecordUnavailable",
    "Redaction",
    "RetentionFor",
    "SegmentStore",
    "ThreadIdFor",
    "conversation_attribution",
    "conversation_cell_id",
    "conversation_heads",
    "conversation_key",
    "conversation_pair_key",
    "decode_list_cursor",
    "direct_endpoint",
    "direct_key",
    "direct_seed",
    "encode_list_cursor",
    "entry_key",
    "merge_head_sets",
    "merge_heads",
    "merge_queue_threads",
    "merged_read",
    "no_redaction",
    "page_heads",
    "queue_threads",
    "record_heads",
    "retry_delay_s",
    "row_conversation",
    "row_thread",
    "thread_filtered",
    "visible_messages",
]
