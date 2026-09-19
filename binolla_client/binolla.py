"""
binolla.py
----------
Async Binolla trading client — reverse-engineered, stealth, production-grade.

Usage:
    import asyncio
    from binolla import BinollaClient, DEMO, REAL

    async def main():
        client = BinollaClient("email@example.com", "password")
        await client.login()                        # mandatory first call

        balance = await client.get_balance()
        trade   = await client.place_trade("EURUSD_otc", 1000, 0, duration=60)
        result  = await client.get_trade_result(trade["id"])
        history = await client.get_trade_history()

        await client.disconnect()

    asyncio.run(main())

Notes
-----
- login() is mandatory before any other method. Raises NotLoggedInError otherwise.
- Session is kept alive automatically via JWT proactive refresh + WS ping.
  You never need to call login() again while the client is running.
- No active logout happens server-side during a live session.
  Access token expires in ~minutes; refresh token valid for ~days.
- Rate limit: max 30 concurrent open trades (enforced server-side).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Literal, Optional

import aiohttp
from aiohttp import ClientSession, CookieJar, WSMsgType

logger = logging.getLogger("binolla")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_BASE_URL = "https://binolla.com"
_WS_URL          = "wss://ws3.binolla.com/socket.io/?EIO=4&transport=websocket"
_WS_URL_FALLBACK = _WS_URL   # same endpoint; slot kept for _reconnect compat

_OK  = "s_"
_ERR = "f_"

_EV_AUTH         = "authorization"
_EV_ACCOUNT      = "account/change"
_EV_ORDER_OPEN   = "orders/open"
_EV_ORDER_LIST   = "orders/opened/list"
_EV_ORDER_CLOSED = "orders/closed/list"
_EV_PING         = "ping"

# userAccountType values from the live frontend enum.
# real=0, demo=1, trial=2, battle=3, forex=9.
# account_mode here is the binary tradeMode/uaid: demo=0, real=1.
_UAT = {0: 1, 1: 0, 2: 2, 3: 3, 9: 9}

DEMO = 0
REAL = 1

Direction = Literal[0, 1]  # Binolla protocol: high=0, low=1

# Fixed-time trade amount limits from the Binolla frontend currency rules.
# Values are in the account currency and are validated before any WS order send.
_TRADE_AMOUNT_LIMITS = {
    "USD": (1, 1_000),
    "BRL": (5, 5_000),
    "IDR": (14_000, 14_000_000),
    "MYR": (5, 5_000),
    "THB": (35, 35_000),
    "BDT": (110, 110_000),
    "KES": (1_600, 1_600_000),
    "ZAR": (20, 20_000),
    "NGN": (1_500, 900_000),
    "GHS": (12, 12_000),
    "PKR": (290, 290_000),
}

# Stealth: mimic Chrome 125 on Windows
_HTTP_HEADERS = {
    "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept":             "application/json, text/plain, */*",
    "Accept-Language":    "en-US,en;q=0.9",
    "Accept-Encoding":    "gzip, deflate, br",
    "Sec-Ch-Ua":          '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile":   "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest":     "empty",
    "Sec-Fetch-Mode":     "cors",
    "Sec-Fetch-Site":     "same-origin",
    "Referer":            "https://binolla.com/trading/demo",
}

_WS_HEADERS = {
    "User-Agent":      _HTTP_HEADERS["User-Agent"],
    "Origin":          "https://binolla.com",
    "Accept-Language": "en-US,en;q=0.9",
}


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class BinollaError(Exception):
    """Base exception."""

class NotLoggedInError(BinollaError):
    """Raised when any method is called before login()."""

class AuthError(BinollaError):
    """HTTP login or WS authentication failed."""

class TradeError(BinollaError):
    """Trade placement or result retrieval failed."""

class ConnectionError(BinollaError):
    """WebSocket connection issue."""


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────

class BinollaClient:
    """
    Async Binolla trading client.

    Public methods
    --------------
    login()                      — authenticate (must call first)
    get_balance()    -> dict     — {real, bonus, currency}
    get_profile()    -> dict     — full user/account state
    switch_account(mode)         — DEMO (0) or REAL (1)
    place_trade(...) -> dict     — open a trade, returns deal object
    get_trade_result(id) -> dict — waits for trade to close, returns result
    get_open_trades()   -> list  — currently open deals
    get_trade_history() -> list  — closed deals history
    disconnect()                 — clean shutdown
    """

    def __init__(
        self,
        email: str,
        password: str,
        currency: str = "NGN",
        account_mode: int = DEMO,
    ) -> None:
        self._email        = email
        self._password     = password
        self._account_mode = account_mode

        self._jar:  CookieJar               = CookieJar()
        self._http: Optional[ClientSession] = None

        self._access_token:     Optional[str] = None
        self._refresh_token:    Optional[str] = None
        self._token_expires_at: float         = 0.0
        self._currency = currency.upper().strip() if currency else "NGN"
        if self._currency not in _TRADE_AMOUNT_LIMITS:
            raise ValueError(f"Unsupported currency '{self._currency}'. Supported: {', '.join(_TRADE_AMOUNT_LIMITS)}")
        # Currency is supplied at construction; the server balance response may override it.

        # These are separate protocol fields.
        # For demo, the browser sends uaid=0 and userAccountType=1.
        self._uaid:              int = 0 if account_mode == DEMO else account_mode
        self._user_account_type: int = _UAT.get(account_mode, 1)

        self._ws:        Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_authed: bool = False

        self._open_trades:   list[dict] = []
        self._closed_trades: list[dict] = []
        self._assets: dict[str, Any] = {}
        self._demo_balance: Optional[float] = None
        self._real_balance: Optional[float] = None

        self._listeners: dict[str, list[asyncio.Future]] = {}

        # Socket.IO binary-event state. A `45...` text packet is followed by
        # one or more raw WebSocket binary frames that fill its placeholders.
        self._sio_binary_packet: Optional[list[Any]] = None
        self._sio_binary_expected: int = 0
        self._sio_binary_received: dict[int, bytes] = {}

        self._recv_task:    Optional[asyncio.Task] = None
        self._ping_task:    Optional[asyncio.Task] = None
        self._refresh_task: Optional[asyncio.Task] = None

        self._logged_in = False

    # ── context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "BinollaClient":
        await self.login()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────────

    async def login(self) -> None:
        """
        Authenticate with Binolla and open the WebSocket.
        Must be called before any other method.
        """
        self._http = ClientSession(
            cookie_jar=self._jar,
            headers=_HTTP_HEADERS,
        )
        await self._http_login()
        await self._ws_connect()
        self._logged_in = True
        logger.info(
            "BinollaClient ready. Mode: %s",
            "DEMO" if self._account_mode == DEMO else "REAL",
        )

    async def get_balance(self) -> dict:
        """Return {real, bonus, currency}."""
        self._require_login()
        data = await self._get("/api/finance/balance")
        server_currency = (data.get("currency") or "").upper().strip()
        if server_currency in _TRADE_AMOUNT_LIMITS:
            self._currency = server_currency
        real = data.get("realAmount", 0)
        bonus = data.get("bonusAmount", 0)
        try:
            self._real_balance = float(real or 0)
        except (TypeError, ValueError):
            self._real_balance = None
        return {
            "real":     real,
            "bonus":    bonus,
            "currency": data.get("currency", ""),
        }

    async def get_profile(self) -> dict:
        """Return full /api/state user object."""
        self._require_login()
        return await self._get("/api/state")

    async def switch_account(self, mode: int) -> None:
        """Switch between DEMO (0) and REAL (1); protocol account type is separate."""
        self._require_login()
        if mode not in (DEMO, REAL):
            raise BinollaError(f"Unsupported account mode '{mode}'. Use DEMO (0) or REAL (1).")
        self._account_mode       = mode
        self._uaid               = 0 if mode == DEMO else mode
        self._user_account_type  = _UAT.get(mode, 1)
        await self._ws_send(_EV_ACCOUNT, {"userAccountType": self._user_account_type, "uaid": self._uaid})
        logger.info("Switched to %s.", "DEMO" if mode == DEMO else "REAL")

    async def place_trade(
        self,
        asset: str,
        amount: float,
        direction: Direction,
        *,
        duration: Optional[int] = None,
        close_time: Optional[int] = None,
        risk_free_id: Optional[int] = None,
        source: Optional[int] = None,
    ) -> dict:
        """Open a fixed-time trade using caller-supplied trade settings.

        ``direction`` is the protocol ``cmd`` value:
            0 = Higher (frontend ``_C.high``)
            1 = Lower  (frontend ``_C.low``)

        The caller chooses the asset, amount, direction and expiry. The
        method only builds the protocol payload and sends it.
        """
        self._require_login()

        if direction not in (0, 1):
            raise TradeError(f"direction/cmd must be 0 (Higher) or 1 (Lower), got '{direction}'.")
        if duration is None and close_time is None:
            raise TradeError("Provide 'duration' (seconds) or 'close_time' (UTC timestamp).")
        if duration is not None and close_time is not None:
            raise TradeError("Provide only one of 'duration' or 'close_time'.")

        # Gate predictable frontend/server rejections before any WS order send.
        if not self._currency or self._currency not in _TRADE_AMOUNT_LIMITS:
            raise TradeError(
                f"Cannot validate trade amount: unsupported account currency '{self._currency}'."
            )

        limits = _TRADE_AMOUNT_LIMITS[self._currency]
        min_amount, max_amount = limits
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            raise TradeError(f"amount must be a number, got '{amount}'.")
        if amount < min_amount:
            raise TradeError(
                f"Trade blocked locally: amount {amount} is below the {self._currency} minimum {min_amount}."
            )
        if amount > max_amount:
            raise TradeError(
                f"Trade blocked locally: amount {amount} exceeds the {self._currency} maximum {max_amount}."
            )
        if round(float(amount), 2) != float(amount):
            raise TradeError("Trade blocked locally: amount supports at most 2 decimal places.")

        asset = str(asset).strip()
        if not asset:
            raise TradeError("Trade blocked locally: asset cannot be empty.")
        if self._assets and asset not in self._assets:
            raise TradeError(f"Trade blocked locally: unknown/unlisted asset '{asset}'.")

        available = self._demo_balance if self._account_mode == DEMO else self._real_balance
        if available is not None and float(amount) > available:
            raise TradeError(
                f"Trade blocked locally: amount {amount} exceeds available "
                f"{self._currency} balance {available}."
            )

        if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool)):
            raise TradeError(f"duration must be an integer number of seconds, got '{duration}'.")
        if close_time is not None and (not isinstance(close_time, int) or isinstance(close_time, bool)):
            raise TradeError(f"close_time must be an integer UTC timestamp, got '{close_time}'.")

        if duration is not None and (duration < 60 or duration > 14_400):
            raise TradeError(
                f"Trade blocked locally: duration must be between 60 and 14400 seconds, got {duration}."
            )

        payload: dict[str, Any] = {
            "asset": asset,
            "amount": amount,
            "cmd": direction,
        }

        if risk_free_id is not None:
            payload["riskFreeId"] = risk_free_id
        if source is not None:
            payload["source"] = source

        payload["duration" if duration is not None else "time"] = (
            duration if duration is not None else close_time
        )

        ok_fut  = self._one_shot(f"{_OK}{_EV_ORDER_OPEN}")
        err_fut = self._one_shot(f"{_ERR}{_EV_ORDER_OPEN}")

        await self._ws_send(_EV_ORDER_OPEN, payload)

        done, pending = await asyncio.wait(
            [asyncio.ensure_future(ok_fut), asyncio.ensure_future(err_fut)],
            timeout=12,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        if not done:
            raise TradeError("Timed out waiting for trade confirmation (12s).")

        result = done.pop().result()
        if isinstance(result, dict) and "error" in result:
            raise TradeError(f"Trade rejected: {result['error']}")

        deal    = result.get("deal", result) if isinstance(result, dict) else result
        deal_id = deal.get("id") or deal.get("uuid") or deal.get("ticket", "?")
        logger.info("Trade placed: cmd=%s %s x%s | id=%s", direction, asset, amount, deal_id)
        return deal

    async def get_trade_result(
        self,
        trade_id: str,
        timeout: float = 300.0,
    ) -> dict:
        """
        Wait until a trade closes and return its result.

        Returns {id, profit, status, openPrice, closePrice, raw}
        """
        self._require_login()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            for t in self._closed_trades:
                tid = str(t.get("id") or t.get("uuid") or t.get("ticket", ""))
                if tid == str(trade_id):
                    profit = float(t.get("profit", 0))
                    return {
                        "id":         tid,
                        "profit":     profit,
                        "status":     "win" if profit > 0 else ("draw" if profit == 0 else "loss"),
                        "openPrice":  t.get("openPrice"),
                        "closePrice": t.get("closePrice"),
                        "raw":        t,
                    }
            await asyncio.sleep(0.4)

        raise TradeError(f"Trade {trade_id} result not received within {timeout}s.")

    async def get_open_trades(self) -> list:
        """Request and return currently open trades."""
        self._require_login()
        fut = self._one_shot(f"{_OK}{_EV_ORDER_LIST}")
        await self._ws_send(_EV_ORDER_LIST, None)
        result = await asyncio.wait_for(fut, timeout=12)
        if isinstance(result, list):
            self._open_trades = result
        return self._open_trades

    async def get_trade_history(self) -> list:
        """Request and return closed trade history."""
        self._require_login()
        fut = self._one_shot(f"{_OK}{_EV_ORDER_CLOSED}")
        await self._ws_send(_EV_ORDER_CLOSED, None)
        result = await asyncio.wait_for(fut, timeout=12)
        if isinstance(result, list):
            self._closed_trades = result
        return self._closed_trades

    async def disconnect(self) -> None:
        """Clean shutdown."""
        for task in (self._ping_task, self._recv_task, self._refresh_task):
            if task and not task.done():
                task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._http and not self._http.closed:
            await self._http.close()
        self._logged_in = False
        logger.info("BinollaClient disconnected.")

    # ─────────────────────────────────────────────────────────────────────────
    # AUTH — HTTP LAYER
    # ─────────────────────────────────────────────────────────────────────────

    async def _http_login(self) -> None:
        """
        POST /api/auth/login → sets session cookie.
        GET  /api/state      → extract JWT (cookie now active, returns authed state).
        Token structure: state["token"]["accessToken"] + state["token"]["refreshToken"]
        """
        logger.info("Logging in as %s ...", self._email)

        # Step 1 — POST login (sets session cookie on success)
        login_data = await self._post("/api/auth/login", {
            "email":    self._email,
            "password": self._password,
        })
        logger.debug("Login POST response keys: %s", list(login_data.keys()))

        # Token may come directly in the login response
        token_obj = login_data.get("token")

        # Step 2 — GET /api/state with the session cookie now active
        # This returns the authenticated state including the JWT
        state = await self._get("/api/state")

        logger.debug("State response keys: %s", list(state.keys()))

        # State token is authoritative — prefer it
        token_obj = state.get("token") or token_obj

        if not token_obj or not isinstance(token_obj, dict):
            # Build a useful error message
            captcha_required = state.get("captchaRequired", False)
            has_user         = bool(state.get("user"))
            raise AuthError(
                f"No JWT token received. "
                f"captchaRequired={captcha_required}, user_present={has_user}. "
                f"Login keys={list(login_data.keys())}, State keys={list(state.keys())}. "
                + (
                    "CAPTCHA is required — log in once via the browser to satisfy it."
                    if captcha_required and not has_user else
                    "Check your email and password."
                )
            )

        self._store_tokens(token_obj, server_ts=state.get("timestamp"))

        self._uaid              = self._account_mode
        self._user_account_type = _UAT.get(self._account_mode, "demo")

        logger.info(
            "HTTP login OK. Token expires in ~%.0fs.",
            max(self._token_expires_at - time.time(), 0),
        )

    def _store_tokens(self, token_obj: dict, server_ts: Optional[int] = None) -> None:
        self._access_token  = token_obj.get("accessToken") or token_obj.get("access")
        self._refresh_token = token_obj.get("refreshToken") or token_obj.get("refresh")

        if not self._access_token:
            raise AuthError(
                f"accessToken missing. Token object keys: {list(token_obj.keys())}"
            )

        # Decode exp from JWT payload — no external lib needed
        try:
            parts  = self._access_token.split(".")
            padded = parts[1] + "=="
            claims = json.loads(base64.urlsafe_b64decode(padded))
            self._token_expires_at = float(claims.get("exp", 0))
        except Exception:
            self._token_expires_at = time.time() + 900  # fallback: 15 min

    async def _refresh_access_token(self) -> None:
        """Refresh JWT using refresh token. Falls back to full re-login."""
        logger.info("Refreshing access token ...")
        try:
            data = await self._post_bearer("/api/auth/login", {}, bearer=self._refresh_token)
            token_obj = data.get("token") or data
            if isinstance(token_obj, dict) and token_obj.get("accessToken"):
                self._store_tokens(token_obj)
                await self._ws_reauth()
                logger.info("Token refreshed.")
                return
        except Exception as e:
            logger.warning("Refresh-token flow failed (%s). Re-logging in ...", e)

        await self._http_login()
        await self._ws_reauth()

    async def _token_refresh_loop(self) -> None:
        """Background: refresh access token 30s before expiry."""
        try:
            while True:
                wait = max(self._token_expires_at - time.time() - 30, 60)
                await asyncio.sleep(wait)
                await self._refresh_access_token()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Token refresh loop error: %s", e)

    # ─────────────────────────────────────────────────────────────────────────
    # AUTH — WS LAYER
    # ─────────────────────────────────────────────────────────────────────────

    async def _ws_connect(self) -> None:
        logger.info("Opening WebSocket ...")
        last_err = None
        for _url in (_WS_URL, _WS_URL_FALLBACK):
            try:
                self._ws = await self._http.ws_connect(
                    _url,
                    headers=_WS_HEADERS,
                    heartbeat=None,
                    autoclose=False,
                    autoping=False,
                )
                self._active_ws_url = _url
                logger.info("WebSocket connected via %s.", _url)
                last_err = None
                break
            except Exception as e:
                logger.warning("WS %s failed: %s. Trying next ...", _url, e)
                last_err = e
        if last_err is not None:
            raise ConnectionError(f"All WebSocket endpoints failed: {last_err}")

        # Socket.IO handshake — must complete BEFORE recv loop starts
        await self._sio_handshake()

        # Start recv loop FIRST — _ws_auth sends a message and waits for a
        # response, so the loop must already be reading the socket.
        self._recv_task    = asyncio.create_task(self._recv_loop(),          name="binolla-recv")
        self._ping_task    = asyncio.create_task(self._ping_loop(),          name="binolla-ping")
        self._refresh_task = asyncio.create_task(self._token_refresh_loop(), name="binolla-refresh")

        await self._ws_auth()
        await self._ws_send(_EV_ACCOUNT, {"userAccountType": self._user_account_type, "uaid": self._uaid})

    async def _ws_auth(self) -> None:
        """Send WS authorization — payload matches browser exactly."""
        logger.info("Authenticating WebSocket ...")
        ok_fut  = self._one_shot(f"{_OK}{_EV_AUTH}")
        err_fut = self._one_shot(f"{_ERR}{_EV_AUTH}")

        logger.debug(
            "WS AUTH: userAccountType=%s uaid=%s token_present=%s token_len=%s",
            self._user_account_type,
            self._uaid,
            bool(self._access_token),
            len(self._access_token or ""),
        )

        await self._ws_send(_EV_AUTH, {
            "token":           self._access_token,
            "userAccountType": self._user_account_type,
            "uaid":            self._uaid,
        })

        ok_task  = asyncio.ensure_future(ok_fut)
        err_task = asyncio.ensure_future(err_fut)

        done, pending = await asyncio.wait(
            [ok_task, err_task],
            timeout=15,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        if not done:
            raise AuthError("WS auth timed out (15s). Check token and uaid.")

        completed = done.pop()
        if completed is err_task:
            payload = completed.result()
            raise AuthError(
                f"WS auth rejected (f_authorization). "
                f"Payload: {payload}. "
                f"Token may be wrong or account type mismatch."
            )

        self._ws_authed = True
        logger.info("WebSocket authenticated.")

    async def _ws_reauth(self) -> None:
        """Re-authenticate existing socket after token refresh."""
        if not self._ws or self._ws.closed:
            return
        self._ws_authed = False
        ok_fut = self._one_shot(f"{_OK}{_EV_AUTH}")
        await self._ws_send(_EV_AUTH, {
            "token":           self._access_token,
            "userAccountType": self._user_account_type,
            "uaid":            self._uaid,
        })
        try:
            await asyncio.wait_for(ok_fut, timeout=10)
            self._ws_authed = True
            logger.info("WS re-authenticated.")
        except asyncio.TimeoutError:
            logger.warning("WS re-auth timed out.")

    # ─────────────────────────────────────────────────────────────────────────
    # WS I/O
    # ─────────────────────────────────────────────────────────────────────────

    async def _ws_send(self, event: str, payload: Any) -> None:
        if not self._ws or self._ws.closed:
            raise ConnectionError("WebSocket is not connected.")
        frame = [event] if payload is None else [event, payload]
        await self._ws.send_str("42" + json.dumps(frame))  # SIO message prefix

    async def _recv_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await self._dispatch_binary(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    logger.warning("WS closed: %s", msg.type)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("recv_loop error: %s", e)
        finally:
            self._ws_authed = False

            if self._logged_in:
                asyncio.create_task(self._reconnect())
                
    async def _dispatch(self, raw: str) -> None:
        logger.debug("WS RECV: %s", raw[:300])

        # ── Socket.IO control frames ──────────────────────────────────────────
        if raw == "2":                              # EIO ping → pong
            if self._ws and not self._ws.closed:
                asyncio.create_task(self._ws.send_str("3"))
            return
        if raw[:2] in ("0{", "40", "41"):           # EIO open / SIO ns-ack / ns-disconnect
            return
        # Socket.IO binary event: `45` + JSON packet, followed by raw binary
        # attachment frame(s). Keep the packet until the attachments arrive.
        if raw.startswith("45"):
            # Format: 45N-<json>, where N is attachment count.
            if len(raw) < 4 or not raw[2].isdigit() or raw[3] != "-":
                logger.warning("WS invalid SIO binary header: %s", raw[:100])
                return
            attachments = int(raw[2])
            packet = raw[4:]
            try:
                msg = json.loads(packet)
            except json.JSONDecodeError:
                logger.warning("WS invalid SIO binary packet: %s", raw[:200])
                return

            if not isinstance(msg, list) or not msg:
                logger.warning("WS unexpected SIO binary packet: %s", raw[:200])
                return

            self._sio_binary_packet = msg
            self._sio_binary_expected = attachments
            self._sio_binary_received = {}

            if attachments == 0:
                await self._dispatch_sio_message(self._sio_binary_packet[0])
                self._sio_binary_packet = None
            return

        # ── SIO message: strip "42" prefix ───────────────────────────────────
        if raw.startswith("42"):
            raw = raw[2:]

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("WS non-JSON frame: %s", raw[:200])
            return
        if not isinstance(msg, list) or not msg:
            logger.warning("WS unexpected format: %s", raw[:200])
            return

        await self._dispatch_sio_message(msg)

    async def _dispatch_binary(self, data: bytes) -> None:
        if self._sio_binary_packet is None:
            logger.warning("WS unexpected binary attachment: %d bytes", len(data))
            return

        # Socket.IO attachment order is the placeholder `num` value.
        packet = self._sio_binary_packet
        received = self._sio_binary_received

        def replace(value: Any) -> Any:
            if isinstance(value, dict) and value.get("_placeholder") is True:
                num = value.get("num")
                if isinstance(num, int) and num in received:
                    blob = received[num]
                    try:
                        return json.loads(blob.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return blob
                return value
            if isinstance(value, list):
                return [replace(v) for v in value]
            if isinstance(value, dict):
                return {k: replace(v) for k, v in value.items()}
            return value

        num = len(received)
        received[num] = bytes(data)

        if len(received) < self._sio_binary_expected:
            return

        completed = replace(packet)
        self._sio_binary_packet = None
        self._sio_binary_expected = 0
        self._sio_binary_received = {}

        await self._dispatch_sio_message(completed)

    async def _dispatch_sio_message(self, msg: list[Any]) -> None:
        event: str = msg[0]
        payload: Any = msg[1] if len(msg) > 1 else None
        logger.debug("WS event=%s payload_keys=%s", event, list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)

        if event == f"{_OK}assets/list":
            logger.info("RAW %s payload: %s", event, payload)
            if isinstance(payload, list):
                self._assets = {
                    str(item[1]): item
                    for item in payload
                    if isinstance(item, (list, tuple)) and len(item) > 1 and item[1]
                }
        elif event == f"{_OK}balances/list":
            logger.info("RAW %s payload: %s", event, payload)
            if isinstance(payload, dict):
                try:
                    self._demo_balance = float(payload.get("demoBalance", 0) or 0)
                except (TypeError, ValueError):
                    self._demo_balance = None
                try:
                    self._real_balance = float(payload.get("liveBalance", 0) or 0)
                except (TypeError, ValueError):
                    self._real_balance = None
        elif event == f"{_OK}settings/list":
            logger.info("RAW %s payload: %s", event, payload)
        elif event == f"{_OK}orders/opened/list" and isinstance(payload, list):
            self._open_trades = payload
        elif event == f"{_OK}orders/closed/list" and isinstance(payload, list):
            self._closed_trades = payload
        elif event == f"{_OK}orders/open":
            deal = payload.get("deal", payload) if isinstance(payload, dict) else payload
            if isinstance(deal, dict):
                self._open_trades.append(deal)
        elif event == f"{_OK}deauthorization":
            code = payload.get("code") if isinstance(payload, dict) else None
            if code == 1002:
                logger.warning("WS token expired (1002). Refreshing ...")
                asyncio.create_task(self._refresh_access_token())

        futures = self._listeners.pop(event, [])
        for fut in futures:
            if not fut.done():
                fut.set_result(payload)

    async def _sio_handshake(self) -> None:
        """
        Complete Socket.IO EIO4 handshake on a freshly connected socket.
        Must be called before starting _recv_loop.
        """
        # Step 1 — read EIO4 open packet: "0{sid, pingInterval, ...}"
        msg = await asyncio.wait_for(self._ws.receive(), timeout=6)
        raw = msg.data if isinstance(msg.data, str) else msg.data.decode(errors="ignore")
        logger.debug("SIO EIO open: %s", raw[:80])
        if not raw.startswith("0"):
            raise ConnectionError(f"Expected EIO4 open packet, got: {raw[:80]}")

        # Step 2 — send namespace connect: "40"
        await self._ws.send_str("40")

        # Step 3 — read until namespace ack arrives ("40{...}")
        deadline = asyncio.get_event_loop().time() + 6
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=1)
                raw = msg.data if isinstance(msg.data, str) else ""
                if raw.startswith("40"):
                    logger.debug("SIO namespace ack: %s", raw[:60])
                    return
            except asyncio.TimeoutError:
                break
        raise ConnectionError("Socket.IO namespace connect timed out.")

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(25)
                if self._ws and not self._ws.closed:
                    await self._ws_send(_EV_PING, None)
        except asyncio.CancelledError:
            pass

    async def _reconnect(self) -> None:
        logger.info("Reconnecting in 3s ...")
        await asyncio.sleep(3)
        try:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            last_err = None
            for _url in (getattr(self, "_active_ws_url", _WS_URL), _WS_URL, _WS_URL_FALLBACK):
                try:
                    self._ws = await self._http.ws_connect(
                        _url,
                        headers=_WS_HEADERS,
                        heartbeat=None,
                        autoclose=False,
                        autoping=False,
                    )
                    self._active_ws_url = _url
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
            if last_err is not None:
                raise last_err
            await self._sio_handshake()
            await self._ws_auth()
            await self._ws_send(_EV_ACCOUNT, {"userAccountType": self._user_account_type, "uaid": self._uaid})
            logger.info("Reconnected.")
        except Exception as e:
            logger.error("Reconnect failed: %s. Retrying in 10s ...", e)
            await asyncio.sleep(10)
            asyncio.create_task(self._reconnect())

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _require_login(self) -> None:
        if not self._logged_in:
            raise NotLoggedInError(
                "Call await client.login() before using any other method."
            )

    def _one_shot(self, event: str) -> asyncio.Future:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._listeners.setdefault(event, []).append(fut)
        return fut

    def _auth_header(self, bearer: Optional[str] = None) -> dict:
        token = bearer or self._access_token
        return {"AUTHORIZATION": f"Bearer {token}"} if token else {}

    async def _get(self, path: str) -> dict:
        async with self._http.get(
            f"{_BASE_URL}{path}",
            headers=self._auth_header(),
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _post(self, path: str, body: dict) -> dict:
        async with self._http.post(
            f"{_BASE_URL}{path}",
            json=body,
            headers={**self._auth_header(), "Content-Type": "application/json"},
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _post_bearer(self, path: str, body: dict, bearer: Optional[str] = None) -> dict:
        async with self._http.post(
            f"{_BASE_URL}{path}",
            json=body,
            headers={**self._auth_header(bearer), "Content-Type": "application/json"},
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)
