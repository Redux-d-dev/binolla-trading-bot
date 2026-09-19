from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

from aiogram.fsm.state import State, StatesGroup

from binolla_client.binolla import BinollaClient


# ── FSM state groups ──────────────────────────────────────────────────────────

class AuthStates(StatesGroup):
    email    = State()
    password = State()


class ConfigStates(StatesGroup):
    channel         = State()
    channel_type    = State()
    waiting_webapp  = State()   # waiting for Mini App auth to complete
    account_mode    = State()
    amount          = State()
    martingale      = State()


class SettingsStates(StatesGroup):
    reauth_email    = State()
    reauth_password = State()
    channel         = State()
    channel_type    = State()
    waiting_webapp  = State()   # waiting for Mini App auth to complete
    amount          = State()
    martingale      = State()


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ActiveTrade:
    trade_id: str
    expiry:   float        # unix timestamp when trade closes
    amount:   float
    level:    int          # martingale level that was used


@dataclass
class UserConfig:
    email:       str
    password:    str
    channel:     str       # @username or invite url
    channel_id:  Optional[int]
    is_private:  bool
    use_demo:    bool
    base_amount: float
    multipliers: list[float]


@dataclass
class UserSession:
    user_id:          int
    config:           UserConfig
    binolla:          BinollaClient
    telethon:         Optional[Any]       = None
    martingale_level: int                 = 0
    active_trade:     Optional[ActiveTrade] = None
    seen_message_ids: set                 = field(default_factory=set)
    queue:            asyncio.Queue       = field(default_factory=asyncio.Queue)
    worker_task:      Optional[asyncio.Task] = None


# ── Global in-memory registries ───────────────────────────────────────────────

sessions:         dict[int, UserSession] = {}   # user_id  → session
channel_watchers: dict[int, list[int]]   = {}   # chan_id  → [user_ids]
pending_clients:  dict[int, Any]         = {}   # user_id  → BinollaClient (during config)
pending_telethon: dict[int, dict]        = {}   # user_id  → {client, phone, hash}
webapp_contexts:  dict[int, dict]        = {}   # user_id  → FSMContext (for webapp callback)
