# ⚡ Binolla Trading Bot

> Automated binary options trading bot — listens to any Telegram signal channel and executes trades on Binolla hands-free, with a built-in Martingale engine.

https://github.com/user-attachments/assets/REPLACE_WITH_YOUR_VIDEO_ID.mp4

---

## What It Does

- Monitors any Telegram signal channel (public or private) in real time via Telethon
- Parses signals automatically — extracts asset, direction (UP/DOWN), and duration
- Executes trades instantly via a **custom reverse-engineered Binolla WebSocket client** (no public API exists)
- Martingale engine — auto-scales trade amount on loss, resets on win
- Demo / Live account switching without restart
- Full trade history and real-time P&L — all inside Telegram

---

## How It Works

```
Telegram Signal Channel
        ↓
  Telethon Listener
        ↓
   Signal Parser  ──→  invalid signal? dropped.
        ↓
 Martingale Engine  (calculates trade amount for current level)
        ↓
 Binolla WebSocket Client  ──→  Trade Placed
        ↓
  Result Watcher  (polls after expiry)
        ↓
 Telegram Notification  (✅ Won / ❌ Lost + next level)
```

---

## Features

| Feature | Status |
|---|---|
| Any Telegram channel — public or private | ✅ |
| Custom reverse-engineered Binolla API client | ✅ |
| Martingale strategy with configurable multipliers | ✅ |
| Demo and Live account mode, switchable live | ✅ |
| Telegram Mini App OTP auth flow | ✅ |
| Per-user session management (multi-user ready) | ✅ |
| Dedup signal queue (no duplicate trades) | ✅ |
| Trade history — latest / last 20 with P&L | ✅ |
| Real-time balance display | ✅ |
| In-chat notifications for every trade event | ✅ |
| Cloudflare tunnel for local dev | ✅ |
| Render deployment ready | ✅ |

---

## Stack

| Layer | Technology |
|---|---|
| Telegram bot | aiogram 3 |
| Channel listener | Telethon |
| Binolla client | Custom async WebSocket (reverse-engineered) |
| Mini App server | FastAPI + uvicorn |
| HTTP / WS client | aiohttp |
| Tunnel (local) | Cloudflare tunnel (auto-started) |
| Deployment | Render |

---

## Project Structure

```
├── binolla_client/
│   └── binolla.py       # Reverse-engineered async Binolla WebSocket client
├── main.py              # Entry point — bot + server + tunnel, all in one process
├── handlers.py          # Telegram FSM handlers and full menu logic
├── trading.py           # Signal parser, Martingale engine, trade queue worker
├── listener.py          # Telethon listener — attaches to signal channels
├── state.py             # Session and config data models, global registries
├── keyboards.py         # aiogram inline and reply keyboards
├── webapp.py            # FastAPI Mini App server (Telegram OTP auth)
├── config.py            # Env config + Cloudflare tunnel auto-start
└── requirements.txt
```

---

## Setup

### 1. Clone

```bash
git clone https://github.com/YOUR_USERNAME/binolla-trading-bot.git
cd binolla-trading-bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Fill in your values
```

### 4. Run

```bash
python main.py
```

Cloudflare tunnel starts automatically for local runs.
For production, deploy to Render — set the `RENDER` env variable and it switches to `RENDER_EXTERNAL_URL` automatically.

---

## Environment Variables

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token — from @BotFather |
| `TELETHON_API_ID` | From [my.telegram.org](https://my.telegram.org) |
| `TELETHON_API_HASH` | From [my.telegram.org](https://my.telegram.org) |
| `WEBAPP_PORT` | Port for Mini App server (default: `850`) |
| `RENDER_EXTERNAL_URL` | Auto-set by Render on cloud deployment |

---

## The Binolla Client

`binolla_client/binolla.py` is a fully async, reverse-engineered WebSocket client for Binolla — built from scratch since no public API exists.

Capabilities:
- JWT authentication with proactive token refresh (session never drops)
- WebSocket connection with auto-reconnect and keepalive ping
- Real-time balance push via socket events
- Trade placement with full parameter control
- Trade result polling after expiry
- Demo / Live account switching mid-session

---

## Bot Flow (User Perspective)

```
/start
  → Enter Binolla email + password   (login via reverse-engineered client)
  → Enter signal channel              (@channel or invite link)
  → Select public / private
  → Authenticate Telegram account     (Mini App OTP flow)
  → Select Demo or Live mode
  → Set base trade amount
  → Set Martingale multipliers        (e.g. 1, 2, 4, 8, 16)

Bot is now live. Every signal → auto-trade → result notification.
```

Main menu:
- 💰 Balance — live demo + real balance
- 📊 Trade History — latest or last 20 with P&L summary
- 🔄 Switch Account — toggle demo/live instantly
- ⚙️ Settings — update amount, Martingale, channel, or re-auth

---

## Built By

**Redux** — Independent developer specializing in trading bots, automation systems, and fintech infrastructure.

Open to building similar or custom systems.

[![Upwork](https://img.shields.io/badge/Hire_on-Upwork-6fda44?style=flat-square)](YOUR_UPWORK_LINK)
[![Twitter](https://img.shields.io/badge/Follow-Twitter-1da1f2?style=flat-square)](YOUR_TWITTER_LINK)
