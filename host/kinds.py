"""The fixed value sets of the store's text columns, one Literal each. Imports nothing from host/."""

from __future__ import annotations

from typing import Literal, get_args

FinalStatus = Literal["ok", "failed", "rate_limited", "killed", "interrupted"]
RunStatus = Literal["running", FinalStatus]
RUN_STATUSES: tuple[RunStatus, ...] = get_args(RunStatus)
FINAL_STATUSES: tuple[FinalStatus, ...] = get_args(FinalStatus)

MessageKind = Literal["text", "event"]
MESSAGE_KINDS: tuple[MessageKind, ...] = get_args(MessageKind)

ConversationKind = Literal["user", "pair", "group"]
CONVERSATION_KINDS: tuple[ConversationKind, ...] = get_args(ConversationKind)

BotState = Literal["idle", "queued", "working", "over_budget"]
