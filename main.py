import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
from handlers import router
from webapp import app as fastapi_app, set_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-16s  %(levelname)s  %(message)s",
)
logging.getLogger("binolla").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


BANNER = """
╔══════════════════════════════════════════════════════╗
║                                                      ║
║        ⚡  BINOLLA TRADING BOT  ⚡                  ║
║              Automated Signal Executor               ║
║                                                      ║
╠══════════════════════════════════════════════════════╣
║                                                      ║
║   🤖  Telegram Bot       →  CONNECTED               ║
║   📡  Channel Listener   →  READY                   ║
║   💹  Martingale Engine  →  LOADED                  ║
║   ⚙️   Signal Parser      →  ACTIVE                  ║
║   🌐  Mini App Server    →  RUNNING                 ║
║                                                      ║
║   Status:  🟢  LIVE — READY TO TRADE                ║
║                                                      ║
╚══════════════════════════════════════════════════════╝
"""


async def main() -> None:
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    set_bot(bot)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.get_me()

    server = uvicorn.Server(
        uvicorn.Config(
            fastapi_app,
            host="0.0.0.0",
            port=config.WEBAPP_PORT,
            log_level="warning",
        )
    )
    print(BANNER)

    await asyncio.gather(
        dp.start_polling(bot),
        server.serve(),
        config.set_webapp_url(),
    )


if __name__ == "__main__":
    asyncio.run(main())
