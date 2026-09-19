from __future__ import annotations

import logging

from telethon import TelegramClient, events
import os
import config
from state import channel_watchers, pending_telethon, sessions
from trading import enqueue_signal, parse_signal

logger = logging.getLogger("listener")

_clients: dict[int, TelegramClient] = {}  # user_id → authenticated TelegramClient

current_folder = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(current_folder, "Sessions"), exist_ok=True)

async def request_code(user_id: int, phone: str) -> None:
    """Connect a Telethon client and send OTP to the given phone number."""
    
    client = TelegramClient(
        os.path.join(current_folder, f"Sessions/tg_{user_id}"), config.TELETHON_API_ID, config.TELETHON_API_HASH
    )
    await client.connect()
    result = await client.send_code_request(phone)
    _clients[user_id] = client
    pending_telethon[user_id] = {
        "client": client,
        "phone":  phone,
        "hash":   result.phone_code_hash,
    }


async def sign_in(user_id: int, code: str) -> TelegramClient:
    data = pending_telethon.get(user_id)  # get, not pop
    if not data:
        raise RuntimeError("No pending OTP session for this user.")
    client: TelegramClient = data["client"]
    await client.sign_in(data["phone"], code, phone_code_hash=data["hash"])
    pending_telethon.pop(user_id)  # only pop after success
    _clients[user_id] = client
    return client


async def attach_listener(user_id: int, channel_username: str) -> int:
    """
    Resolve channel and attach a NewMessage event handler.
    Returns the resolved channel_id.
    """
    client = _clients.get(user_id)
    if not client or not client.is_connected():
        raise RuntimeError("Telethon client not connected for this user.")

    entity     = await client.get_entity(channel_username)
    channel_id: int = entity.id

    channel_watchers.setdefault(channel_id, [])
    if user_id not in channel_watchers[channel_id]:
        channel_watchers[channel_id].append(user_id)

    @client.on(events.NewMessage(chats=channel_id))
    async def _on_message(event):
        text   = event.message.message or ""
        signal = parse_signal(text)
        if not signal:
            return
        for uid in list(channel_watchers.get(channel_id, [])):
            sess = sessions.get(uid)
            if sess:
                enqueue_signal(sess, event.message.id, signal)

    logger.info(
        "Telethon listener attached: %s (id=%d) for user=%d",
        channel_username, channel_id, user_id,
    )
    return channel_id


def get_client(user_id: int) -> TelegramClient | None:
    return _clients.get(user_id)
