"""Conversation state machine.

The bot has a small set of states per Telegram user. v1 stores state in an
in-memory dict — fine for a single-process bot. A Supabase-backed
`SessionStore` is sketched at the bottom as the next step.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class State(str, Enum):
    IDLE = "idle"
    WAITING_FOR_TYPE = "waiting_for_type"
    WAITING_FOR_FIELDS = "waiting_for_fields"
    WAITING_FOR_REFINEMENT = "waiting_for_refinement"
    CONFIRMING_DRAFT = "confirming_draft"


@dataclass(slots=True)
class Session:
    user_id: int
    chat_id: int
    state: State = State.IDLE
    context: dict[str, Any] = field(default_factory=dict)
    last_draft: str | None = None
    last_sources: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    """In-memory session store. Replace with a Supabase-backed version
    if you need to survive restarts and run multiple bot instances."""

    def __init__(self) -> None:
        self._by_user: dict[int, Session] = {}
        self._by_chat: dict[int, Session] = {}

    async def get_or_create(self, user_id: int, chat_id: int) -> Session:
        sess = self._by_user.get(user_id)
        if sess is None:
            sess = Session(user_id=user_id, chat_id=chat_id)
            self._by_user[user_id] = sess
        self._by_chat[chat_id] = sess
        return sess

    async def by_chat(self, chat_id: int) -> Session | None:
        return self._by_chat.get(chat_id)

    async def reset(self, user_id: int) -> None:
        sess = self._by_user.get(user_id)
        if sess is not None:
            sess.state = State.IDLE
            sess.context = {}
            sess.last_draft = None
            sess.last_sources = []


# TODO: SupabaseSessionStore using the `chat_sessions` table. Schema is
# already in db/schema.sql — just wire it up when you need it.
#
# class SupabaseSessionStore:
#     def __init__(self, client: Client) -> None:
#         self.client = client
#
#     async def get_or_create(self, user_id, chat_id) -> Session:
#         # SELECT ... FROM chat_sessions WHERE telegram_user_id = ...
#         #   AND closed_at IS NULL
#         # if none, INSERT and return.
#         # if found, hydrate Session.
#         ...
