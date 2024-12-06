"""Enums for messaging."""

from enum import Enum


class MessageDirection(str, Enum):
    """Message direction."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"


class DialogStatus(str, Enum):
    """Dialog status."""

    active = "active"
    closed = "closed"
    blocked = "blocked"
    rejected = "rejected"
    not_qualified = "not_qualified"
    meeting_scheduled = "meeting_scheduled"
    stopped = "stopped"
