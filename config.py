import asyncio
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# create a folder in the current folder
Path(__file__).parent.absolute().joinpath("Logs").mkdir(exist_ok=True)
ngrok_logger = logging.getLogger("ngrok")
ngrok_logger.setLevel(logging.ERROR)
ngrok_file_handler = logging.FileHandler(
    Path(__file__).parent / "Logs" / "ngrok.log", encoding="utf-8"
)
ngrok_logger.addHandler(ngrok_file_handler)
ngrok_logger.propagate = False

load_dotenv()
BOT_TOKEN         = os.environ["BOT_TOKEN"]
TELETHON_API_ID   = int(os.environ["TELETHON_API_ID"])
TELETHON_API_HASH = os.environ["TELETHON_API_HASH"]
WEBAPP_PORT       = int(os.environ.get("WEBAPP_PORT", "850"))

async def set_webapp_url():
    if not os.getenv("RENDER"):
        import subprocess, re
        await asyncio.sleep(2)

        _cf = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{WEBAPP_PORT}", "--no-autoupdate"],
            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True,
        )
        global WEBAPP_URL
        for line in _cf.stderr:
            m = re.search(r"https://[\w-]+\.trycloudflare\.com", line)
            if m:
                WEBAPP_URL = m.group(0)
                print(f"  cloudflare tunnel: {WEBAPP_URL}\n")
                break
            
    else:
        WEBAPP_URL        = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/") or None
