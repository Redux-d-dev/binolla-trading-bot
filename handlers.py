from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from binolla_client.binolla import DEMO, REAL, BinollaClient

import config
import keyboards as kb
import listener as tg_listener
import state as st
from state import (
    AuthStates, ConfigStates, SettingsStates,
    UserConfig, UserSession,
    channel_watchers, pending_clients, sessions, webapp_contexts,
)
from trading import current_amount, enqueue_signal, parse_signal, queue_worker

logger = logging.getLogger("handlers")
router = Router()


# ─────────────────────────────────────────────────────── helpers ──────────────

def _make_notify(bot: Bot, chat_id: int):
    async def notify(text: str):
        try:
            await bot.send_message(chat_id, text)
        except Exception as exc:
            logger.error("notify failed for %d: %s", chat_id, exc)
    return notify



async def _start_session(
    user_id: int,
    cfg: UserConfig,
    binolla: BinollaClient,
    bot: Bot,
) -> UserSession:
    old = sessions.get(user_id)
    if old:
        if old.worker_task and not old.worker_task.done():
            old.worker_task.cancel()
        old_cid = old.config.channel_id
        if old_cid and old_cid in channel_watchers:
            channel_watchers[old_cid] = [
                u for u in channel_watchers[old_cid] if u != user_id
            ]

    session = UserSession(user_id=user_id, config=cfg, binolla=binolla)
    sessions[user_id] = session
    session.worker_task = asyncio.create_task(
        queue_worker(session, _make_notify(bot, user_id))
    )
    return session


def _update_session_channel(
    user_id: int,
    session: UserSession,
    channel: str,
    is_private: bool,
    channel_id: Optional[int],
) -> None:
    old_cid = session.config.channel_id
    if old_cid and old_cid in channel_watchers:
        channel_watchers[old_cid] = [
            u for u in channel_watchers[old_cid] if u != user_id
        ]
    session.config.channel    = channel
    session.config.is_private = is_private
    session.config.channel_id = channel_id
    if channel_id:
        channel_watchers.setdefault(channel_id, [])
        if user_id not in channel_watchers[channel_id]:
            channel_watchers[channel_id].append(user_id)


def _fmt_balances(binolla: BinollaClient) -> str:
    cur  = getattr(binolla, "_currency", "NGN")
    demo = getattr(binolla, "_demo_balance", None)
    real = getattr(binolla, "_real_balance", None)
    d    = f"{demo:,.2f}" if demo is not None else "—"
    r    = f"{real:,.2f}" if real is not None else "—"
    return f"🎮 Demo: <b>{d} {cur}</b>\n💵 Live: <b>{r} {cur}</b>"


def _fmt_history(trades: list, label: str) -> str:
    if not trades:
        return f"📊 <b>{label}</b>\n\nNo trades found."

    lines = [f"📊 <b>{label} ({len(trades)})</b>\n──────────────────"]
    wins = losses = 0
    net = 0.0

    for i, t in enumerate(trades, 1):
        asset    = t.get("asset", "?")
        profit   = float(t.get("profit", 0))
        cmd      = t.get("command", t.get("cmd", t.get("direction", -1)))
        arrow    = "↑ UP" if cmd == 0 else ("↓ DOWN" if cmd == 1 else "?")
        open_ts  = t.get("openTimestamp", 0)
        close_ts = t.get("closeTimestamp", 0)
        dur_m    = round((close_ts - open_ts) / 60) if close_ts and open_ts else "?"
        open_time = t.get("openTime", "")[-8:] if t.get("openTime") else "?"
        amount   = float(t.get("amount", 0))

        if profit > 0:
            emoji = "✅"
            pnl   = f"+{profit:,.0f} NGN"
            wins += 1
        else:
            emoji = "❌"
            pnl   = f"-{amount:,.0f} NGN"
            losses += 1
        net += profit if profit > 0 else -amount

        lines.append(
            f"{i}. {emoji} {asset}  {arrow}\n"
            f"   💰 {pnl} | ⏱ {dur_m}m | 🕐 {open_time}"
        )

    net_str = f"+{net:,.0f}" if net >= 0 else f"{net:,.0f}"
    lines.append(
        f"──────────────────\n"
        f"✅ Won: {wins}  ❌ Lost: {losses}\n"
        f"💰 Net: {net_str} NGN"
    )
    return "\n".join(lines)


def _session_or_none(user_id: int) -> Optional[UserSession]:
    return sessions.get(user_id)


# ─────────────────────────────────────────────── /start  /cancel ──────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id in sessions:
        await message.answer("Welcome back!", reply_markup=kb.main_menu())
        return
    await message.answer(
        "👋 <b>Binolla Trading Bot</b>\n\nEnter your Binolla <b>email</b>:"
    )
    await state.set_state(AuthStates.email)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


# ───────────────────────────────────────────────────────── auth FSM ───────────

@router.message(AuthStates.email)
async def auth_email(message: Message, state: FSMContext):
    await state.update_data(email=message.text.strip())
    await message.answer("Enter your <b>password</b>:")
    await state.set_state(AuthStates.password)


@router.message(AuthStates.password)
async def auth_password(message: Message, state: FSMContext):
    data     = await state.get_data()
    email    = data["email"]
    password = message.text.strip()
    user_id  = message.from_user.id

    await message.answer("🔐 Logging in…")
    try:
        binolla = BinollaClient(email, password)
        await binolla.login()
    except Exception as exc:
        await message.answer(f"❌ Login failed: {exc}\n\nEnter your email again:")
        await state.set_state(AuthStates.email)
        return

    pending_clients[user_id] = binolla
    await state.update_data(email=email, password=password)
    await message.answer(
        "✅ Logged in!\n\n"
        "Enter the signal provider channel (e.g. <code>@signals</code> or invite link):"
    )
    await state.set_state(ConfigStates.channel)


# ──────────────────────────────────────────────────────── config FSM ──────────

@router.message(ConfigStates.channel)
async def config_channel(message: Message, state: FSMContext):
    await state.update_data(channel=message.text.strip())
    await message.answer("Channel type?", reply_markup=kb.channel_type_kb())
    await state.set_state(ConfigStates.channel_type)


@router.callback_query(ConfigStates.channel_type, F.data.in_({"ch_public", "ch_private"}))
async def config_channel_type(cb: CallbackQuery, state: FSMContext):
    is_private = cb.data == "ch_private"
    await state.update_data(is_private=is_private)
    await cb.message.edit_reply_markup()
    # Both public and private go through Telethon — one listener, always
    user_id = cb.from_user.id
    webapp_contexts[user_id] = state
    await cb.message.answer(
        "Tap below to authenticate your Telegram account:",
        reply_markup=kb.tg_login_kb(config.WEBAPP_URL, user_id),
    )
    await state.set_state(ConfigStates.waiting_webapp)
    await cb.answer()
            

@router.message(F.web_app_data)
async def on_webapp_data(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        return
    if not data.get("authenticated"):
        return

    current = await state.get_state()

    if current == ConfigStates.waiting_webapp:
        await message.answer(
            "✅ Telegram authenticated!\n\nSelect account mode:",
            reply_markup=kb.account_mode_kb(),
        )
        await state.set_state(ConfigStates.account_mode)

    elif current == SettingsStates.waiting_webapp:
        fsm_data   = await state.get_data()
        channel    = fsm_data.get("channel", "")
        session    = _session_or_none(user_id)
        try:
            channel_id = await tg_listener.attach_listener(user_id, channel)
            if session:
                _update_session_channel(user_id, session, channel, True, channel_id)
            await state.clear()
            await message.answer("✅ Channel updated.", reply_markup=kb.main_menu())
        except Exception as exc:
            await message.answer(f"❌ Failed to attach listener: {exc}")

    webapp_contexts.pop(user_id, None)


@router.callback_query(ConfigStates.account_mode, F.data.in_({"mode_demo", "mode_live"}))
async def config_account_mode(cb: CallbackQuery, state: FSMContext):
    await state.update_data(use_demo=(cb.data == "mode_demo"))
    await cb.message.edit_reply_markup()
    await cb.message.answer(
        "Enter your <b>starting trade amount</b> (e.g. <code>5000</code>):"
    )
    await state.set_state(ConfigStates.amount)
    await cb.answer()


@router.message(ConfigStates.amount)
async def config_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid. Enter a positive number:")
        return
    await state.update_data(base_amount=amount)
    await message.answer(
        "Enter <b>Martingale multipliers</b> (comma-separated):\n"
        "Example: <code>1,2,4,8,16</code>"
    )
    await state.set_state(ConfigStates.martingale)


@router.message(ConfigStates.martingale)
async def config_martingale(message: Message, state: FSMContext, bot: Bot):
    try:
        mults = [float(x.strip()) for x in message.text.split(",")]
        if not mults or any(m <= 0 for m in mults):
            raise ValueError
    except ValueError:
        await message.answer("❌ Use comma-separated positive numbers:")
        return

    data    = await state.get_data()
    await state.clear()
    user_id = message.from_user.id

    email      = data["email"]
    password   = data["password"]
    channel    = data["channel"]
    is_private = data.get("is_private", False)
    use_demo   = data.get("use_demo", True)
    base_amt   = data["base_amount"]

    binolla = pending_clients.pop(user_id, None)
    if binolla is None:
        await message.answer("❌ Session expired. Please /start again.")
        return

    target_mode = DEMO if use_demo else REAL
    if binolla._account_mode != target_mode:
        try:
            await binolla.switch_account(target_mode)
        except Exception as exc:
            logger.warning("switch_account error: %s", exc)

    cfg = UserConfig(
        email=email, password=password,
        channel=channel, channel_id=None,
        is_private=is_private, use_demo=use_demo,
        base_amount=base_amt, multipliers=mults,
    )
    session = await _start_session(user_id, cfg, binolla, bot)

    # Attach channel listener — always via Telethon, public or private
    channel_id: Optional[int] = None
    note = ""
    try:
        channel_id = await tg_listener.attach_listener(user_id, channel)
        note = "📡 Telethon listener active."
    except Exception as exc:
        note = f"⚠️ Channel listener error: {exc}"

    if channel_id:
        session.config.channel_id = channel_id

    await asyncio.sleep(0.5)  # give WS time to push initial balances

    multi_str = " → ".join(f"×{m:g}" for m in mults)
    mode_lbl  = "🎮 Demo" if use_demo else "💵 Live"

    await message.answer(
        f"✅ <b>Setup complete!</b>\n\n"
        f"{_fmt_balances(binolla)}\n\n"
        f"<b>Mode:</b> {mode_lbl}\n"
        f"<b>Channel:</b> {channel}\n"
        f"<b>Base amount:</b> {base_amt:,.0f}\n"
        f"<b>Martingale:</b> {multi_str}\n\n"
        f"{note}",
        reply_markup=kb.main_menu(),
    )


# ───────────────────────────────────────────────────────── main menu ──────────

@router.message(F.text == "💰 Balance")
async def menu_balance(message: Message):
    session = _session_or_none(message.from_user.id)
    if not session:
        await message.answer("No active session. /start first.")
        return
    try:
        await session.binolla.get_balance()
    except Exception:
        pass
    mode = "🎮 Demo" if session.config.use_demo else "💵 Live"
    await message.answer(
        f"💰 <b>Balance</b>\n\n{_fmt_balances(session.binolla)}\n\n<b>Active mode:</b> {mode}"
    )


@router.message(F.text == "📊 Trade History")
async def menu_history(message: Message):
    if not _session_or_none(message.from_user.id):
        await message.answer("No active session. /start first.")
        return
    await message.answer("Select range:", reply_markup=kb.trade_history_kb())


@router.message(F.text == "⚙️ Settings")
async def menu_settings(message: Message):
    if not _session_or_none(message.from_user.id):
        await message.answer("No active session. /start first.")
        return
    await message.answer("⚙️ <b>Settings</b>", reply_markup=kb.settings_kb())


@router.message(F.text == "🔄 Switch Account")
async def menu_switch(message: Message):
    session = _session_or_none(message.from_user.id)
    if not session:
        await message.answer("No active session. /start first.")
        return
    mode_lbl = "🎮 Demo" if session.config.use_demo else "💵 Live"
    await message.answer(
        f"{_fmt_balances(session.binolla)}\n\n<b>Current mode:</b> {mode_lbl}",
        reply_markup=kb.switch_account_kb(session.config.use_demo),
    )


# ───────────────────────────────────────────────────── inline callbacks ───────

@router.callback_query(F.data == "do_switch")
async def cb_do_switch(cb: CallbackQuery):
    session = _session_or_none(cb.from_user.id)
    if not session:
        await cb.answer("No session.", show_alert=True)
        return
    new_mode = REAL if session.config.use_demo else DEMO
    try:
        await session.binolla.switch_account(new_mode)
        session.config.use_demo = (new_mode == DEMO)
        mode_lbl = "🎮 Demo" if session.config.use_demo else "💵 Live"
        await cb.message.edit_text(
            f"Switched to <b>{mode_lbl}</b>\n\n{_fmt_balances(session.binolla)}",
            reply_markup=kb.switch_account_kb(session.config.use_demo),
        )
    except Exception as exc:
        await cb.answer(f"Failed: {exc}", show_alert=True)
    await cb.answer()


@router.callback_query(F.data.in_({"hist_latest", "hist_all"}))
async def cb_history(cb: CallbackQuery):
    session = _session_or_none(cb.from_user.id)
    if not session:
        await cb.answer("No session.", show_alert=True)
        return
    await cb.answer()
    try:
        trades = await session.binolla.get_trade_history()
    except Exception as exc:
        await cb.message.answer(f"❌ Error fetching history: {exc}")
        return
    # new
    if cb.data == "hist_latest":
        subset, label = trades[-1:], "Latest Trade"
    else:
        subset, label = trades[-20:], "Last 20 Trades"
        
    await cb.message.answer(_fmt_history(subset, label))


# ───────────────────────────────────────────────── settings callbacks ─────────

@router.callback_query(F.data == "set_reauth")
async def cb_set_reauth(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.answer("Enter new Binolla <b>email</b>:")
    await state.set_state(SettingsStates.reauth_email)


@router.callback_query(F.data == "set_amount")
async def cb_set_amount(cb: CallbackQuery, state: FSMContext):
    if not _session_or_none(cb.from_user.id):
        await cb.answer("No session.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer("Enter new starting trade amount:")
    await state.set_state(SettingsStates.amount)


@router.callback_query(F.data == "set_martingale")
async def cb_set_martingale(cb: CallbackQuery, state: FSMContext):
    if not _session_or_none(cb.from_user.id):
        await cb.answer("No session.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(
        "Enter new Martingale multipliers (comma-separated):\n"
        "Example: <code>1,2,4,8,16</code>"
    )
    await state.set_state(SettingsStates.martingale)


@router.callback_query(F.data == "set_channel")
async def cb_set_channel(cb: CallbackQuery, state: FSMContext):
    if not _session_or_none(cb.from_user.id):
        await cb.answer("No session.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer("Enter new target channel:")
    await state.set_state(SettingsStates.channel)


# ─────────────────────────────────────────────── settings FSM handlers ────────

@router.message(SettingsStates.reauth_email)
async def settings_reauth_email(message: Message, state: FSMContext):
    await state.update_data(email=message.text.strip())
    await message.answer("Enter new <b>password</b>:")
    await state.set_state(SettingsStates.reauth_password)


@router.message(SettingsStates.reauth_password)
async def settings_reauth_password(message: Message, state: FSMContext):
    session = _session_or_none(message.from_user.id)
    if not session:
        await state.clear()
        await message.answer("No session. /start first.")
        return
    data     = await state.get_data()
    email    = data["email"]
    password = message.text.strip()
    await message.answer("🔐 Re-authenticating…")
    try:
        new_binolla = BinollaClient(
            email, password,
            account_mode=DEMO if session.config.use_demo else REAL,
        )
        await new_binolla.login()
    except Exception as exc:
        await message.answer(f"❌ Failed: {exc}")
        await state.clear()
        return
    try:
        await session.binolla.disconnect()
    except Exception:
        pass
    session.binolla          = new_binolla
    session.config.email     = email
    session.config.password  = password
    await state.clear()
    await message.answer("✅ Re-authenticated.", reply_markup=kb.main_menu())


@router.message(SettingsStates.amount)
async def settings_amount(message: Message, state: FSMContext):
    session = _session_or_none(message.from_user.id)
    if not session:
        await state.clear()
        return
    try:
        amount = float(message.text.strip().replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid. Enter a positive number:")
        return
    session.config.base_amount = amount
    session.martingale_level   = 0
    await state.clear()
    await message.answer(f"✅ Base amount updated to {amount:,.0f}.", reply_markup=kb.main_menu())


@router.message(SettingsStates.martingale)
async def settings_martingale(message: Message, state: FSMContext):
    session = _session_or_none(message.from_user.id)
    if not session:
        await state.clear()
        return
    try:
        mults = [float(x.strip()) for x in message.text.split(",")]
        if not mults or any(m <= 0 for m in mults):
            raise ValueError
    except ValueError:
        await message.answer("❌ Use comma-separated positive numbers:")
        return
    session.config.multipliers = mults
    session.martingale_level   = 0
    await state.clear()
    multi_str = " → ".join(f"×{m:g}" for m in mults)
    await message.answer(f"✅ Martingale updated: {multi_str}", reply_markup=kb.main_menu())


@router.message(SettingsStates.channel)
async def settings_channel(message: Message, state: FSMContext):
    await state.update_data(channel=message.text.strip())
    await message.answer("Channel type?", reply_markup=kb.channel_type_kb())
    await state.set_state(SettingsStates.channel_type)


@router.callback_query(SettingsStates.channel_type, F.data.in_({"ch_public", "ch_private"}))
async def settings_channel_type(cb: CallbackQuery, state: FSMContext):
    is_private = cb.data == "ch_private"
    await state.update_data(is_private=is_private)
    await cb.message.edit_reply_markup()
    # Both public and private go through Telethon — one listener, always
    user_id = cb.from_user.id
    webapp_contexts[user_id] = state
    await cb.message.answer(
        "Tap below to authenticate your Telegram account:",
        reply_markup=kb.tg_login_kb(config.WEBAPP_URL, user_id),
    )
    await state.set_state(SettingsStates.waiting_webapp)
    await cb.answer()


