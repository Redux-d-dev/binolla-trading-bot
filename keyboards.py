from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Balance"),   KeyboardButton(text="📊 Trade History")],
            [KeyboardButton(text="⚙️ Settings"),  KeyboardButton(text="🔄 Switch Account")],
        ],
        resize_keyboard=True,
    )


def channel_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔓 Public",  callback_data="ch_public"),
        InlineKeyboardButton(text="🔒 Private", callback_data="ch_private"),
    ]])


def account_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎮 Demo", callback_data="mode_demo"),
        InlineKeyboardButton(text="💵 Live", callback_data="mode_live"),
    ]])


def trade_history_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🕐 Latest",       callback_data="hist_latest"),
        InlineKeyboardButton(text="📋 All (max 20)", callback_data="hist_all"),
    ]])


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Re-authenticate",   callback_data="set_reauth")],
        [InlineKeyboardButton(text="📡 Change Channel",    callback_data="set_channel")],
        [InlineKeyboardButton(text="💵 Change Amount",     callback_data="set_amount")],
        [InlineKeyboardButton(text="📈 Change Martingale", callback_data="set_martingale")],
    ])


def tg_login_kb(webapp_url: str, user_id: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(
            text="🔐 Login to Telegram",
            web_app=WebAppInfo(url=f"{webapp_url}?user_id={user_id}"),
        )]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def switch_account_kb(use_demo: bool) -> InlineKeyboardMarkup:
    label = "Switch to 💵 Live" if use_demo else "Switch to 🎮 Demo"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data="do_switch"),
    ]])
