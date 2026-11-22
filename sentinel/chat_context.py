"""Bounded, opt-in context from text already admitted for moderation."""

from collections import OrderedDict, deque
from dataclasses import dataclass
from uuid import uuid4


MAX_MESSAGES = 100
MAX_MESSAGE_CHARS = 512
MAX_TOTAL_CHARS = 8000
MAX_GROUPS = 128
MAX_PENDING = 512


def validate_context_messages(value):
    """Validate one global or per-group count; zero means the context is off."""
    if type(value) is not int or not 0 <= value <= MAX_MESSAGES:
        raise ValueError("invalid context message limit")
    return value


@dataclass(frozen=True)
class ContextEntry:
    speaker: str
    seconds_before: int
    text: str
    shortened: bool


@dataclass(frozen=True)
class ContextSnapshot:
    entries: tuple[ContextEntry, ...]
    limited: bool

    def audit(self):
        return {"status": "used" if self.entries else "empty",
                "count": len(self.entries), "chars": sum(len(entry.text) for entry in self.entries),
                "limited": self.limited}

    def prompt_data(self):
        return {**self.audit(), "messages": [
            {"speaker": entry.speaker, "seconds_before": entry.seconds_before, "text": entry.text,
             "shortened": entry.shortened} for entry in self.entries]}


@dataclass(frozen=True)
class _Observed:
    received: float
    message_id: str
    sender: str
    text: str
    shortened: bool


class ChatContext:
    """Keep immutable snapshots in memory; only an opaque handle enters SQLite.

    Missing handles (restart or eviction) never pick up new history.
    This object is owned by the runtime's event loop.
    """

    def __init__(self):
        self._groups = OrderedDict()
        self._pending = OrderedDict()
        self._closed = False

    def prepare(self, message, limit):
        if self._closed:
            raise RuntimeError("context store is closed")
        key = (message.platform_id, message.group_id)
        cutoff = message.received_at
        candidates = sorted(
            (entry for entry in self._groups.get(key, ()) if entry.received < cutoff
             and entry.message_id != message.message_id),
            key=lambda entry: entry.received, reverse=True,
        )
        picked = []
        remaining = MAX_TOTAL_CHARS
        for entry in candidates[:limit]:
            if not remaining:
                break
            text = entry.text[:remaining]
            remaining -= len(text)
            picked.append((entry, text))
        aliases = {message.sender_id: "trigger_sender"}
        rows = tuple(ContextEntry(
            aliases.setdefault(entry.sender, f"member_{len(aliases)}"),
            int(cutoff - entry.received), text, entry.shortened or len(text) != len(entry.text),
        ) for entry, text in reversed(picked))
        snapshot = ContextSnapshot(rows, len(picked) < len(candidates) or any(row.shortened for row in rows))
        token = uuid4().hex
        self._pending[token] = ((*key, message.message_id), snapshot)
        if len(self._pending) > MAX_PENDING:
            self._pending.popitem(last=False)
        return token

    def remember(self, message):
        if self._closed or not message.is_text or message.image_urls or not message.text.strip():
            return
        key = (message.platform_id, message.group_id)
        # Keep one extra row so the largest limit can still see MAX_MESSAGES messages before the trigger.
        entries = self._groups.setdefault(key, deque(maxlen=MAX_MESSAGES + 1))
        entries.append(_Observed(message.received_at, message.message_id, message.sender_id,
                                 message.text[:MAX_MESSAGE_CHARS], len(message.text) > MAX_MESSAGE_CHARS))
        self._groups.move_to_end(key)
        if len(self._groups) > MAX_GROUPS:
            self._groups.popitem(last=False)

    def discard(self, token):
        self._pending.pop(token, None)

    def take(self, item):
        token = item.payload.get("context_token")
        if not isinstance(token, str):
            return None
        saved = self._pending.get(token)
        if not saved or saved[0] != (item.platform_id, item.group_id, item.message_id):
            return None
        self.discard(token)
        return saved[1]

    def close(self):
        self._closed = True
        self._pending.clear()
        self._groups.clear()
