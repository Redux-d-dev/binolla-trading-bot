from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Awaitable, Callable, Optional

from state import ActiveTrade, UserSession

logger = logging.getLogger("trading")


# ── Signal parser ─────────────────────────────────────────────────────────────

def parse_signal(text: str) -> Optional[dict]:
    print(f"Parsing signal: {text}")
    asset_m = re.search(
        r"([A-Z]{2,6}[/_][A-Z]{2,6}|[A-Z]{4,8})\s*\(OTC\)",
        text, re.IGNORECASE
    )
    dur_m = re.search(r"(\d+)\s*MINUTES?", text, re.IGNORECASE)
    dir_m = re.search(r"\b(UP|DOWN)\b", text, re.IGNORECASE)

    if not asset_m or not dur_m or not dir_m:
        return None

    asset     = asset_m.group(1).upper().replace("/", "") + "_otc"
    duration  = int(dur_m.group(1)) * 60
    direction = 0 if dir_m.group(1).upper() == "UP" else 1

    return {"asset": asset, "direction": direction, "duration": duration}


# ── Martingale helpers ────────────────────────────────────────────────────────

def current_amount(session: UserSession) -> float:
    lvl = min(session.martingale_level, len(session.config.multipliers) - 1)
    return session.config.base_amount * session.config.multipliers[lvl]


# ── Trade result handler ──────────────────────────────────────────────────────

async def _handle_result(
    session:   UserSession,
    trade_id:  str,
    notify:    Callable[[str], Awaitable],
    asset:     str,
    direction: int,
) -> None:
    active = session.active_trade
    if not active:
        return

    wait = max(active.expiry - time.time(), 0)
    await asyncio.sleep(wait + 1.5)

    try:
        await session.binolla.get_trade_history()
    except Exception:
        pass

    try:
        result = await session.binolla.get_trade_result(trade_id, timeout=5)
        profit = result["profit"]
        status = result["status"]
        arrow  = "↑ UP" if direction == 0 else "↓ DOWN"
        closed = datetime.now().strftime("%H:%M:%S")

        if status == "win":
            session.martingale_level = 0
            await notify(
                f"✅ <b>Trade Won</b>\n"
                f"──────────────────\n"
                f"📈 {asset}  {arrow}\n"
                f"💰 Profit   : +{profit:,.0f} NGN\n"
                f"📊 Level    : Reset → 1\n"
                f"🕐 Closed   : {closed}"
            )
        else:
            session.martingale_level = min(
                session.martingale_level + 1,
                len(session.config.multipliers) - 1,
            )
            next_amt = current_amount(session)
            await notify(
                f"❌ <b>Trade Lost</b>\n"
                f"──────────────────\n"
                f"📈 {asset}  {arrow}\n"
                f"💰 Loss     : -{abs(profit):,.0f} NGN\n"
                f"📊 Next     : Level {session.martingale_level + 1} | {next_amt:,.0f} NGN\n"
                f"🕐 Closed   : {closed}"
            )

    except Exception as exc:
        logger.error("Result fetch error [%s]: %s", trade_id, exc)
        await notify(f"⚠️ Could not confirm trade result: {exc}")
    finally:
        session.active_trade = None


# ── Queue worker ──────────────────────────────────────────────────────────────

async def queue_worker(
    session: UserSession,
    notify:  Callable[[str], Awaitable],
) -> None:
    while True:
        signal = await session.queue.get()
        try:
            if session.active_trade and time.time() < session.active_trade.expiry:
                logger.info("[user=%d] Signal dropped — active trade not expired", session.user_id)
                continue

            amount    = current_amount(session)
            asset     = signal["asset"]
            direction = signal["direction"]
            duration  = signal["duration"]

            try:
                deal = await session.binolla.place_trade(
                    asset, amount, direction, duration=duration
                )
                trade_id = str(
                    deal.get("id") or deal.get("uuid") or deal.get("ticket", "?")
                )
                session.active_trade = ActiveTrade(
                    trade_id=trade_id,
                    expiry=time.time() + duration,
                    amount=amount,
                    level=session.martingale_level,
                )
                arrow = "↑ UP" if direction == 0 else "↓ DOWN"
                now   = datetime.now().strftime("%H:%M:%S")
                await notify(
                    f"🚀 <b>Trade Opened</b>\n"
                    f"──────────────────\n"
                    f"📈 {asset}  {arrow}\n"
                    f"⏱ Duration : {duration // 60} min\n"
                    f"💰 Amount   : {amount:,.0f} NGN\n"
                    f"📊 Level    : {session.martingale_level + 1}/{len(session.config.multipliers)}\n"
                    f"🕐 Time     : {now}"
                )
                asyncio.create_task(_handle_result(session, trade_id, notify, asset, direction))

            except Exception as exc:
                logger.error("Trade execution error: %s", exc)
                await notify(f"❌ Trade failed: {exc}")
        finally:
            session.queue.task_done()


# ── Dedup-safe enqueue ────────────────────────────────────────────────────────

def enqueue_signal(session: UserSession, message_id: int, signal: dict) -> bool:
    if message_id in session.seen_message_ids:
        return False
    session.seen_message_ids.add(message_id)
    session.queue.put_nowait({**signal, "message_id": message_id})
    return True