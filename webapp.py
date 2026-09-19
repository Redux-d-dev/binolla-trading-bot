from __future__ import annotations

import json
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import listener as tg_listener
from state import sessions, webapp_contexts

logger = logging.getLogger("webapp")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_bot_ref = None   # set from main.py after bot is created


def set_bot(bot):
    global _bot_ref
    _bot_ref = bot


# ── API endpoints ─────────────────────────────────────────────────────────────

class OtpRequest(BaseModel):
    user_id: int
    phone: str


class SubmitRequest(BaseModel):
    user_id: int
    code: str


@app.post("/get-otp")
async def get_otp(req: OtpRequest):
    try:
        await tg_listener.request_code(req.user_id, req.phone)
        return {"success": True}
    except Exception as exc:
        logger.error("get-otp error [user=%d]: %s", req.user_id, exc)
        return {"success": False, "error": str(exc)}


@app.post("/submit-otp")
async def submit_otp(req: SubmitRequest):
    try:
        await tg_listener.sign_in(req.user_id, req.code)
        return {"success": True}
    except Exception as exc:
        logger.error("submit-otp error [user=%d]: %s", req.user_id, exc)
        return {"success": False, "error": str(exc)}


# ── Mini App HTML ─────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Telegram Login</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--tg-theme-bg-color, #fff);
    color: var(--tg-theme-text-color, #000);
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 24px;
  }
  .card {
    width: 100%; max-width: 360px;
    display: flex; flex-direction: column; gap: 16px;
  }
  h2 { font-size: 1.2rem; font-weight: 600; text-align: center; }
  p.sub { font-size: 0.85rem; text-align: center;
          color: var(--tg-theme-hint-color, #888); }
  input {
    width: 100%; padding: 12px 14px; border-radius: 10px;
    border: 1.5px solid var(--tg-theme-hint-color, #ccc);
    background: var(--tg-theme-secondary-bg-color, #f5f5f5);
    color: var(--tg-theme-text-color, #000);
    font-size: 1rem; outline: none;
  }
  input:focus { border-color: var(--tg-theme-button-color, #2481cc); }
  button {
    width: 100%; padding: 13px; border-radius: 10px; border: none;
    background: var(--tg-theme-button-color, #2481cc);
    color: var(--tg-theme-button-text-color, #fff);
    font-size: 1rem; font-weight: 600; cursor: pointer;
  }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  #countdown {
    text-align: center; font-size: 0.9rem; font-weight: 600;
    color: var(--tg-theme-button-color, #2481cc);
  }
  #countdown.expired { color: #e53935; }
  #error { color: #e53935; font-size: 0.85rem; text-align: center; }
  #success-msg {
    text-align: center; font-size: 1.1rem; font-weight: 600;
    color: #2e7d32;
  }
  .hidden { display: none !important; }
</style>
</head>
<body>
<div class="card">
  <h2>🔐 Telegram Login</h2>
  <p class="sub">Authenticate your Telegram account<br>to listen to private channels.</p>

  <!-- Step 1: Phone -->
  <div id="step1">
    <div style="display:flex; gap:8px;">
      <input id="phone" type="tel" placeholder="+2348012345678" style="flex:1">
      <button id="btn-otp" style="width:auto; padding:12px 16px; white-space:nowrap;">
        Get OTP
      </button>
    </div>
    <div id="error" class="hidden"></div>
  </div>

  <!-- Step 2: OTP -->
  <div id="step2" class="hidden">
    <input id="otp" type="number" placeholder="Enter OTP code">
    <div id="countdown">5:00</div>
    <button id="btn-submit">Submit</button>
    <div id="error2" class="hidden"></div>
  </div>

  <!-- Success -->
  <div id="step3" class="hidden">
    <div id="success-msg">✅ Authenticated!<br>Returning to bot…</div>
  </div>
</div>

<script>
  const tg = window.Telegram.WebApp;
  tg.ready();
  tg.expand();

  const params  = new URLSearchParams(window.location.search);
  const userId  = parseInt(params.get("user_id") || "0");
  const base    = window.location.origin;

  let countdownInterval = null;

  function showError(elId, msg) {
    const el = document.getElementById(elId);
    el.textContent = msg;
    el.classList.remove("hidden");
  }
  function clearError(elId) {
    const el = document.getElementById(elId);
    el.textContent = "";
    el.classList.add("hidden");
  }

  // ── Get OTP ──────────────────────────────────────────────────────
  document.getElementById("btn-otp").addEventListener("click", async () => {
    const phone = document.getElementById("phone").value.trim();
    if (!phone) { showError("error", "Enter a phone number first."); return; }
    clearError("error");
    const btn = document.getElementById("btn-otp");
    btn.disabled = true;
    btn.textContent = "Sending…";

    try {
      const res  = await fetch(base + "/get-otp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, phone }),
      });
      const data = await res.json();
      if (data.success) {
        document.getElementById("step1").classList.add("hidden");
        document.getElementById("step2").classList.remove("hidden");
        startCountdown(300);
      } else {
        showError("error", data.error || "Failed to send OTP.");
        btn.disabled = false;
        btn.textContent = "Get OTP";
      }
    } catch (e) {
      showError("error", "Network error. Try again.");
      btn.disabled = false;
      btn.textContent = "Get OTP";
    }
  });

  // ── Submit OTP ───────────────────────────────────────────────────
  document.getElementById("btn-submit").addEventListener("click", async () => {
    const code = document.getElementById("otp").value.trim();
    if (!code) { showError("error2", "Enter the OTP code."); return; }
    clearError("error2");
    const btn = document.getElementById("btn-submit");
    btn.disabled = true;
    btn.textContent = "Verifying…";

    try {
      const res  = await fetch(base + "/submit-otp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, code }),
      });
      const data = await res.json();
      if (data.success) {
        clearInterval(countdownInterval);
        document.getElementById("step2").classList.add("hidden");
        document.getElementById("step3").classList.remove("hidden");
        tg.sendData(JSON.stringify({ authenticated: true }));
        setTimeout(() => tg.close(), 1500);
      } else {
        showError("error2", data.error || "Invalid OTP. Try again.");
        btn.disabled = false;
        btn.textContent = "Submit";
      }
    } catch (e) {
      showError("error2", "Network error. Try again.");
      btn.disabled = false;
      btn.textContent = "Submit";
    }
  });

  // ── Countdown ────────────────────────────────────────────────────
  function startCountdown(seconds) {
    const el = document.getElementById("countdown");
    countdownInterval = setInterval(() => {
      const m = Math.floor(seconds / 60);
      const s = seconds % 60;
      el.textContent = m + ":" + String(s).padStart(2, "0");
      if (--seconds < 0) {
        clearInterval(countdownInterval);
        el.textContent = "Code expired — go back and request a new one.";
        el.classList.add("expired");
        document.getElementById("btn-submit").disabled = true;
      }
    }, 1000);
  }
</script>
</body>
</html>"""


@app.get("/")
async def serve_webapp():
    return HTMLResponse(content=_HTML)
