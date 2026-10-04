"""Job queue: one background worker renders videos one at a time and reports back to Telegram.

Rendering is CPU/GPU heavy, so jobs run strictly sequentially in a worker thread
while the bot keeps answering commands on the event loop.
"""
import asyncio
import html
import logging
import time
from pathlib import Path

from telegram import Bot
from telegram.error import BadRequest, TelegramError

from .config import AppConfig, PipelineOptions
from .pipeline import Cancelled, PipelineResult, run_pipeline
from .storage import Storage

logger = logging.getLogger(__name__)

PROGRESS_MIN_INTERVAL = 4.0  # seconds between status-message edits (Telegram flood limits)


class JobManager:
    def __init__(self, cfg: AppConfig, storage: Storage, bot: Bot):
        self._cfg = cfg
        self._storage = storage
        self._bot = bot
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._cancel: set[int] = set()
        self._running: int | None = None

    async def submit(self, chat_id: int, source: Path, options: PipelineOptions) -> tuple[int, int]:
        """Queue a job. Returns (job_id, position) where 1 means it starts right away."""
        job_id = await self._storage.add_job(chat_id, str(source), options)
        position = self._queue.qsize() + (1 if self._running is not None else 0) + 1
        await self._queue.put(job_id)
        return job_id, position

    async def cancel(self, job_id: int, chat_id: int) -> bool:
        job = await self._storage.get_job(job_id, chat_id)
        if not job or job.status not in ("queued", "running"):
            return False
        self._cancel.add(job_id)
        if job.status == "queued":
            await self._storage.set_status(job_id, "cancelled")
        return True

    async def run_forever(self) -> None:
        while True:
            job_id = await self._queue.get()
            self._running = job_id  # set before any await so submit() counts it in queue positions
            try:
                await self._run_job(job_id)
            except Exception:  # never let one job kill the worker
                logger.exception("Job %s crashed the runner", job_id)
            finally:
                self._running = None
                self._cancel.discard(job_id)

    # ------------------------------------------------------------------
    async def _run_job(self, job_id: int) -> None:
        job = await self._storage.get_job(job_id)
        if job is None or job.status == "cancelled" or job_id in self._cancel:
            if job is not None:
                self._drop_upload(Path(job.source))
            return
        self._running = job_id
        await self._storage.set_status(job_id, "running")
        chat_id = job.chat_id
        source = Path(job.source)

        status_msg = await self._safe_send(chat_id, f"⏳ Задача #{job_id}: запускаю обработку…")
        loop = asyncio.get_running_loop()
        last_edit = 0.0

        def on_progress(pct: float, msg: str) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit < PROGRESS_MIN_INTERVAL or status_msg is None:
                return
            last_edit = now
            asyncio.run_coroutine_threadsafe(
                self._edit(status_msg, f"⏳ Задача #{job_id}: {pct:.0f}% — {msg}"), loop
            )

        async def edit_final(text: str) -> None:
            if status_msg is not None:
                await self._edit(status_msg, text)
            else:
                await self._safe_send(chat_id, text)

        try:
            result: PipelineResult = await asyncio.to_thread(
                run_pipeline,
                source, job.options, self._cfg.output_dir, self._cfg.work_dir, self._cfg.music_dir,
                self._cfg.whisper_model, on_progress, lambda: job_id in self._cancel,
            )
        except Cancelled:
            await self._storage.set_status(job_id, "cancelled")
            await edit_final(f"🛑 Задача #{job_id} отменена.")
            return
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            await self._storage.set_status(job_id, "failed", error=str(exc)[:1000])
            await edit_final(f"❌ Задача #{job_id} не удалась:\n{str(exc)[-600:]}")
            return
        finally:
            self._drop_upload(source)

        await self._storage.set_status(job_id, "done", outputs=[str(p) for p in result.outputs])
        await edit_final(f"✅ Задача #{job_id} готова: {len(result.outputs)} шорт(ов).")
        for note in result.notes:
            await self._safe_send(chat_id, f"ℹ️ {note}")
        await self._deliver(chat_id, result)

    def _drop_upload(self, source: Path) -> None:
        """Delete only files the bot itself downloaded (never the user's inbox files)."""
        try:
            uploads = (self._cfg.work_dir / "uploads").resolve()
            if source.resolve().is_relative_to(uploads):
                source.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove upload %s", source)

    async def _deliver(self, chat_id: int, result: PipelineResult) -> None:
        limit = self._cfg.max_upload_mb * 1024 * 1024
        for path in result.outputs:
            if path.stat().st_size > limit:
                await self._safe_send(
                    chat_id,
                    f"📁 {html.escape(path.name)} слишком большой для отправки "
                    f"({path.stat().st_size // 1024 // 1024} МБ). Файл сохранён:\n<code>{html.escape(str(path))}</code>",
                    parse_mode="HTML",
                )
                continue
            try:
                with open(path, "rb") as fh:
                    await self._bot.send_video(
                        chat_id, fh, caption=path.name, supports_streaming=True,
                        width=1080, height=1920, write_timeout=600, read_timeout=120,
                    )
            except TelegramError as exc:
                logger.warning("send_video failed: %s", exc)
                await self._safe_send(chat_id, f"⚠️ Не удалось отправить {path.name}: {exc}\nФайл: {path}")
        if result.diagnostics_png and result.diagnostics_png.exists():
            try:
                with open(result.diagnostics_png, "rb") as fh:
                    await self._bot.send_document(chat_id, fh, caption="Диагностика анализа динамики")
            except TelegramError:
                logger.warning("Could not send diagnostics plot")

    async def _safe_send(self, chat_id: int, text: str, **kwargs):
        try:
            return await self._bot.send_message(chat_id, text, **kwargs)
        except TelegramError as exc:
            logger.warning("send_message to %s failed: %s", chat_id, exc)
            return None

    async def _edit(self, message, text: str) -> None:
        try:
            await message.edit_text(text)
        except BadRequest:
            pass  # "message is not modified" and similar
        except TelegramError as exc:
            logger.debug("edit failed: %s", exc)
