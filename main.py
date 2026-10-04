"""Entry point: Telegram bot + render worker in one asyncio loop (same layout as Vinted_Telegramm_Bot/main.py).

    python main.py
"""
import asyncio
import logging
import sys

from shorts_factory.bot import BOT_COMMANDS, build_application
from shorts_factory.config import load_config
from shorts_factory.ffmpeg_utils import require_ffmpeg
from shorts_factory.jobs import JobManager
from shorts_factory.storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def main() -> None:
    require_ffmpeg()
    cfg = load_config()
    if not cfg.allowed_user_ids:
        logger.warning(
            "ALLOWED_USER_IDS is empty: the bot will refuse everyone. Send /start to the bot "
            "to see your Telegram ID, put it in .env and restart."
        )

    storage = Storage(cfg.db_path)
    await storage.connect()

    app = build_application(cfg, storage)
    jobs = JobManager(cfg, storage, app.bot)
    app.bot_data["jobs"] = jobs

    async with app:
        await app.bot.set_my_commands(BOT_COMMANDS)
        await app.start()
        await app.updater.start_polling()
        logger.info("Bot started. Inbox: %s | Music: %s", cfg.inbox_dir, cfg.music_dir)
        try:
            await jobs.run_forever()
        finally:
            await app.updater.stop()
            await app.stop()
            await storage.close()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
