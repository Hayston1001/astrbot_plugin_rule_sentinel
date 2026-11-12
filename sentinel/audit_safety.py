"""Minimize untrusted response data before including it in audit records."""

import re
import unicodedata


def normalize_for_echo(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in text if not char.isspace() and unicodedata.category(char) != "Cf")


def safe_resolved_model(value: object, message: str) -> str | None:
    """Keep only plausible identifiers that do not reproduce the full message."""
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", value) is None:
        return None
    normalized_message = normalize_for_echo(message)
    if normalized_message and normalized_message in normalize_for_echo(value):
        return None
    return value
