"""Telegram interface (python-telegram-bot v21, same structure as Vinted_Telegramm_Bot/bot.py).

Flow: send a video (or a YouTube link, or pick a file from /inbox) -> tweak options
on an inline panel -> press Start -> the job queue renders Shorts and sends them back.
"""
import asyncio
import functools
import logging
import secrets
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import audio_mix
from .config import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, AppConfig, PipelineOptions
from .ffmpeg_utils import FFmpegError, probe
from .jobs import JobManager
from .storage import Storage
from .subtitles.brand_kits import PRESETS

logger = logging.getLogger(__name__)

CLOUD_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API getFile limit without a local Bot API server

HELP_TEXT = (
    "🎬 Я превращаю обычное видео в YouTube Shorts: нарезка → вертикальный формат 9:16 → "
    "субтитры с подсветкой слов → фоновая музыка.\n\n"
    "Как пользоваться:\n"
    "• отправьте видео файлом (до 20 МБ), или\n"
    "• отправьте ссылку на видео (YouTube и др.), или\n"
    "• положите большой файл в папку inbox на компьютере и выберите его через /inbox.\n\n"
    "Перед запуском бот покажет панель настроек.\n\n"
    "/settings — настройки по умолчанию\n"
    "/inbox — файлы из папки inbox\n"
    "/jobs — последние задачи\n"
    "/music — музыкальная библиотека (отправьте mp3/wav, чтобы добавить трек)\n"
    "/cancel — отменить активные задачи"
)

BOT_COMMANDS = [
    BotCommand("start", "Справка о боте"),
    BotCommand("settings", "Настройки по умолчанию"),
    BotCommand("inbox", "Выбрать файл из папки inbox"),
    BotCommand("jobs", "Последние задачи"),
    BotCommand("music", "Музыкальная библиотека"),
    BotCommand("cancel", "Отменить активные задачи"),
]

MODE_LABEL = {"highlight": "🔥 Хайлайт (1 шорт)", "slice": "✂️ Нарезка (много шортов)"}
VERTICAL_LABEL = {"blur": "размытый фон", "crop": "обрезка по центру", "off": "без изменений"}
VERTICAL_CYCLE = ["blur", "crop", "off"]
KITS = list(PRESETS)


# --------------------------------------------------------------------------
#  Access control
# --------------------------------------------------------------------------
def restricted(handler):
    """Allow only ALLOWED_USER_IDS. With an empty list nobody is allowed (fail closed)."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        cfg: AppConfig = context.bot_data["cfg"]
        user = update.effective_user
        if user is None or user.id not in cfg.allowed_user_ids:
            logger.warning("Rejected user %s", user.id if user else None)
            target = update.effective_message
            if update.callback_query:
                await update.callback_query.answer("Нет доступа", show_alert=True)
            elif target:
                await target.reply_text("⛔ Нет доступа.")
            return None
        return await handler(update, context, *args, **kwargs)

    return wrapper


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: AppConfig = context.bot_data["cfg"]
    user = update.effective_user
    if user.id not in cfg.allowed_user_ids:
        await update.message.reply_text(
            f"⛔ Нет доступа. Ваш Telegram ID: {user.id}\n"
            "Владелец бота должен добавить его в ALLOWED_USER_IDS в файле .env и перезапустить бота."
        )
        return
    await update.message.reply_text(HELP_TEXT)


# --------------------------------------------------------------------------
#  Options panel
# --------------------------------------------------------------------------
def _onoff(flag: bool) -> str:
    return "✅" if flag else "❌"


def panel_text(opts: PipelineOptions, source_label: str | None) -> str:
    lines = [f"📹 Видео: {source_label}" if source_label else "⚙️ Настройки по умолчанию", ""]
    lines.append(f"Режим: {MODE_LABEL[opts.mode]}")
    if opts.mode == "highlight":
        lines.append(f"Длина хайлайта: ~{opts.target_sec:.0f} с, алгоритм: {opts.variant}")
    else:
        lines.append(f"Длина клипа: {opts.min_clip_sec:.0f}–{opts.max_clip_sec:.0f} с, максимум {opts.max_clips} шт.")
    lines.append(f"Формат 9:16: {VERTICAL_LABEL[opts.vertical]}")
    lines.append(f"Субтитры: {_onoff(opts.subtitles)}  стиль: {opts.brand_kit}")
    lines.append(f"Музыка: {_onoff(opts.music)}  громкость: {opts.music_volume:.0%}")
    return "\n".join(lines)


def panel_keyboard(opts: PipelineOptions, token: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"Режим: {MODE_LABEL[opts.mode]}", callback_data=f"opt:{token}:mode")],
        [
            InlineKeyboardButton(f"Субтитры {_onoff(opts.subtitles)}", callback_data=f"opt:{token}:subtitles"),
            InlineKeyboardButton(f"Музыка {_onoff(opts.music)}", callback_data=f"opt:{token}:music"),
        ],
        [
            InlineKeyboardButton(f"Стиль: {opts.brand_kit}", callback_data=f"opt:{token}:kit"),
            InlineKeyboardButton(f"9:16: {opts.vertical}", callback_data=f"opt:{token}:vertical"),
        ],
        [
            InlineKeyboardButton("Длина −", callback_data=f"opt:{token}:len-"),
            InlineKeyboardButton("Длина +", callback_data=f"opt:{token}:len+"),
            InlineKeyboardButton("Громкость музыки", callback_data=f"opt:{token}:vol"),
        ],
    ]
    if opts.mode == "highlight":
        rows.append([InlineKeyboardButton(f"Алгоритм: {opts.variant}", callback_data=f"opt:{token}:variant")])
    if token != "-":
        rows.append([
            InlineKeyboardButton("▶️ Запустить", callback_data=f"go:{token}"),
            InlineKeyboardButton("✖ Отмена", callback_data=f"drop:{token}"),
        ])
    return InlineKeyboardMarkup(rows)


def _cycle(values: list, current):
    return values[(values.index(current) + 1) % len(values)] if current in values else values[0]


def apply_option(opts: PipelineOptions, field: str) -> None:
    if field == "mode":
        opts.mode = "slice" if opts.mode == "highlight" else "highlight"
    elif field == "subtitles":
        opts.subtitles = not opts.subtitles
    elif field == "music":
        opts.music = not opts.music
    elif field == "kit":
        opts.brand_kit = _cycle(KITS, opts.brand_kit)
    elif field == "vertical":
        opts.vertical = _cycle(VERTICAL_CYCLE, opts.vertical)
    elif field == "variant":
        opts.variant = _cycle(["heuristic", "surprisal", "attention"], opts.variant)
    elif field == "vol":
        opts.music_volume = _cycle([0.1, 0.2, 0.3, 0.5], round(opts.music_volume, 2))
    elif field in ("len+", "len-"):
        step = 15.0 if field == "len+" else -15.0
        if opts.mode == "highlight":
            opts.target_sec = min(180.0, max(15.0, opts.target_sec + step))
        else:
            # Shorts are capped at 3 minutes; keep a sane 15 s spread between bounds
            opts.max_clip_sec = min(180.0, max(30.0, opts.max_clip_sec + step))
            opts.min_clip_sec = max(15.0, min(opts.min_clip_sec, opts.max_clip_sec - 15.0))


async def _show_panel(message, opts: PipelineOptions, token: str, source_label: str | None) -> None:
    await message.reply_text(panel_text(opts, source_label), reply_markup=panel_keyboard(opts, token))


def _pending(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("pending", {})


async def _offer_source(update: Update, context: ContextTypes.DEFAULT_TYPE, path: Path, label: str) -> None:
    storage: Storage = context.bot_data["storage"]
    opts = await storage.get_options(update.effective_chat.id)
    token = secrets.token_hex(3)
    pending = _pending(context)
    if len(pending) >= 10:  # bound memory: forget the oldest offers
        pending.pop(next(iter(pending)))
    pending[token] = {"path": str(path), "opts": opts.to_dict(), "label": label}
    await _show_panel(update.effective_message, opts, token, label)


# --------------------------------------------------------------------------
#  Incoming sources: video file / link / inbox
# --------------------------------------------------------------------------
@restricted
async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: AppConfig = context.bot_data["cfg"]
    msg = update.effective_message
    media = msg.video or msg.document
    if media is None:
        return
    name = getattr(media, "file_name", None) or f"video_{msg.message_id}.mp4"
    if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
        await msg.reply_text("Это не похоже на видеофайл. Поддерживаются: " + ", ".join(sorted(VIDEO_EXTENSIONS)))
        return
    if media.file_size and media.file_size > CLOUD_DOWNLOAD_LIMIT and not cfg.api_base_url:
        await msg.reply_text(
            f"⚠️ Telegram не даёт боту скачать файлы больше 20 МБ ({media.file_size // 1024 // 1024} МБ).\n"
            f"Положите файл в папку:\n{cfg.inbox_dir}\nи выберите его через /inbox. "
            "Либо запустите локальный Bot API сервер (см. README)."
        )
        return

    uploads = cfg.work_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    dest = uploads / f"{update.effective_chat.id}_{msg.message_id}{Path(name).suffix.lower()}"
    status = await msg.reply_text("⬇️ Скачиваю видео…")
    try:
        tg_file = await context.bot.get_file(media.file_id, read_timeout=120)
        await tg_file.download_to_drive(dest, read_timeout=600)
        info = await asyncio.to_thread(probe, dest)
    except Exception as exc:
        logger.exception("Download failed")
        dest.unlink(missing_ok=True)
        await status.edit_text(f"❌ Не удалось скачать или прочитать видео: {exc}")
        return
    await status.delete()
    await _offer_source(update, context, dest, f"{name} ({info['duration'] / 60:.1f} мин)")


@restricted
async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: AppConfig = context.bot_data["cfg"]
    msg = update.effective_message
    url = msg.text.strip().split()[0]
    try:
        import yt_dlp
    except ImportError:
        await msg.reply_text("Для ссылок нужен пакет yt-dlp: pip install yt-dlp")
        return

    uploads = cfg.work_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    stem = f"{update.effective_chat.id}_{msg.message_id}"
    status = await msg.reply_text("⬇️ Скачиваю видео по ссылке…")

    def download() -> tuple[Path, str]:
        opts = {
            "outtmpl": str(uploads / f"{stem}.%(ext)s"),
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=True)
        files = sorted(uploads.glob(f"{stem}.*"))
        if not files:
            raise RuntimeError("yt-dlp не вернул файл")
        return files[0], meta.get("title") or url

    try:
        path, title = await asyncio.to_thread(download)
        info = await asyncio.to_thread(probe, path)
    except Exception as exc:
        for leftover in uploads.glob(f"{stem}.*"):
            leftover.unlink(missing_ok=True)
        await status.edit_text(f"❌ Не удалось скачать: {str(exc)[-300:]}")
        return
    await status.delete()
    await _offer_source(update, context, path, f"{title[:60]} ({info['duration'] / 60:.1f} мин)")


@restricted
async def cmd_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: AppConfig = context.bot_data["cfg"]
    files = sorted(
        (p for p in cfg.inbox_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:20]
    if not files:
        await update.message.reply_text(f"Папка inbox пуста:\n{cfg.inbox_dir}\nПоложите туда видео и повторите /inbox.")
        return
    context.user_data["inbox"] = [str(p) for p in files]
    buttons = [
        [InlineKeyboardButton(f"{p.name[:40]} · {p.stat().st_size // 1024 // 1024} МБ", callback_data=f"in:{i}")]
        for i, p in enumerate(files)
    ]
    await update.message.reply_text("Выберите файл:", reply_markup=InlineKeyboardMarkup(buttons))


@restricted
async def cb_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    files = context.user_data.get("inbox", [])
    idx = int(query.data.split(":")[1])
    if idx >= len(files) or not Path(files[idx]).is_file():
        await query.edit_message_text("Список устарел, повторите /inbox.")
        return
    path = Path(files[idx])
    try:
        info = await asyncio.to_thread(probe, path)
    except FFmpegError as exc:
        await query.edit_message_text(f"❌ Не удалось прочитать файл: {exc}")
        return
    await query.edit_message_text(f"Выбрано: {path.name}")
    await _offer_source(update, context, path, f"{path.name} ({info['duration'] / 60:.1f} мин)")


# --------------------------------------------------------------------------
#  Panel callbacks
# --------------------------------------------------------------------------
@restricted
async def cb_option(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, token, field = query.data.split(":")
    storage: Storage = context.bot_data["storage"]
    chat_id = query.message.chat_id

    if token == "-":  # defaults panel from /settings
        opts = await storage.get_options(chat_id)
        label = None
    else:
        entry = _pending(context).get(token)
        if entry is None:
            await query.edit_message_text("Это предложение устарело. Отправьте видео заново.")
            return
        opts = PipelineOptions.from_dict(entry["opts"])
        label = entry["label"]

    apply_option(opts, field)
    if token == "-":
        await storage.save_options(chat_id, opts)
    else:
        entry["opts"] = opts.to_dict()
        await storage.save_options(chat_id, opts)  # remember choices as the new defaults
    await query.edit_message_text(panel_text(opts, label), reply_markup=panel_keyboard(opts, token))


@restricted
async def cb_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data.split(":")[1]
    entry = _pending(context).pop(token, None)
    if entry is None:
        await query.edit_message_text("Это предложение устарело. Отправьте видео заново.")
        return
    opts = PipelineOptions.from_dict(entry["opts"])
    try:
        opts.validate()
    except ValueError as exc:
        await query.edit_message_text(f"Некорректные настройки: {exc}")
        return
    cfg: AppConfig = context.bot_data["cfg"]
    if opts.music and not audio_mix.list_tracks(cfg.music_dir):
        await query.message.reply_text("ℹ️ В музыкальной библиотеке нет треков — музыка будет пропущена (/music).")
    jobs: JobManager = context.bot_data["jobs"]
    job_id, position = await jobs.submit(query.message.chat_id, Path(entry["path"]), opts)
    where = "начинаю сразу" if position == 1 else f"в очереди, позиция {position}"
    await query.edit_message_text(
        f"📥 Задача #{job_id} принята ({where}).\n\n{panel_text(opts, entry['label'])}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Отменить", callback_data=f"cancel:{job_id}")]]),
    )


@restricted
async def cb_drop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    cfg: AppConfig = context.bot_data["cfg"]
    entry = _pending(context).pop(query.data.split(":")[1], None)
    if entry:
        path = Path(entry["path"])
        if path.resolve().is_relative_to((cfg.work_dir / "uploads").resolve()):
            path.unlink(missing_ok=True)
    await query.edit_message_text("Отменено.")


@restricted
async def cb_cancel_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    jobs: JobManager = context.bot_data["jobs"]
    ok = await jobs.cancel(int(query.data.split(":")[1]), query.message.chat_id)
    await query.answer("Отмена запрошена" if ok else "Задача уже завершена", show_alert=not ok)


# --------------------------------------------------------------------------
#  Misc commands
# --------------------------------------------------------------------------
@restricted
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage: Storage = context.bot_data["storage"]
    opts = await storage.get_options(update.effective_chat.id)
    await _show_panel(update.message, opts, "-", None)


@restricted
async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage: Storage = context.bot_data["storage"]
    jobs = await storage.recent_jobs(update.effective_chat.id)
    if not jobs:
        await update.message.reply_text("Задач пока нет. Отправьте видео.")
        return
    icons = {"queued": "🕓", "running": "⏳", "done": "✅", "failed": "❌", "cancelled": "🛑", "interrupted": "⚠️"}
    lines = []
    for j in jobs:
        line = f"{icons.get(j.status, '•')} #{j.id} {Path(j.source).name[:30]} — {j.status}"
        if j.status == "done":
            line += f" ({len(j.outputs)} шт.)"
        elif j.error:
            line += f"\n    {j.error[-120:]}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage: Storage = context.bot_data["storage"]
    jobs: JobManager = context.bot_data["jobs"]
    cancelled = 0
    for j in await storage.recent_jobs(update.effective_chat.id, limit=10):
        if j.status in ("queued", "running") and await jobs.cancel(j.id, j.chat_id):
            cancelled += 1
    await update.message.reply_text(
        f"Отмена запрошена для задач: {cancelled}" if cancelled else "Активных задач нет."
    )


@restricted
async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: AppConfig = context.bot_data["cfg"]
    tracks = audio_mix.list_tracks(cfg.music_dir)
    names = "\n".join(f"• {t.name}" for t in tracks[:20]) or "—"
    await update.message.reply_text(
        f"🎵 Треков в библиотеке: {len(tracks)}\n{names}\n\n"
        f"Чтобы добавить, отправьте mp3/wav файлом или положите в папку:\n{cfg.music_dir}"
    )


@restricted
async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: AppConfig = context.bot_data["cfg"]
    msg = update.effective_message
    media = msg.audio or msg.document
    name = Path(getattr(media, "file_name", None) or f"track_{msg.message_id}.mp3").name
    if Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
        await msg.reply_text("Поддерживаются только mp3 и wav.")
        return
    if media.file_size and media.file_size > CLOUD_DOWNLOAD_LIMIT and not cfg.api_base_url:
        await msg.reply_text("Файл больше 20 МБ — положите его прямо в папку музыки (/music).")
        return
    dest = cfg.music_dir / name
    tg_file = await context.bot.get_file(media.file_id)
    await tg_file.download_to_drive(dest)
    await msg.reply_text(f"🎵 Трек добавлен: {name}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error", exc_info=context.error)


# --------------------------------------------------------------------------
def build_application(cfg: AppConfig, storage: Storage) -> Application:
    # concurrent_updates: a long link download must not block the "cancel" button or other chats
    builder = Application.builder().token(cfg.telegram_token).concurrent_updates(True)
    if cfg.api_base_url:  # self-hosted Bot API server lifts the 20 MB / 50 MB limits
        base = cfg.api_base_url.rstrip("/")
        builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot").local_mode(True)
    app = builder.build()
    app.bot_data["cfg"] = cfg
    app.bot_data["storage"] = storage

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("inbox", cmd_inbox))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("music", cmd_music))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CallbackQueryHandler(cb_option, pattern=r"^opt:[0-9a-f-]+:[a-z+-]+$"))
    app.add_handler(CallbackQueryHandler(cb_go, pattern=r"^go:[0-9a-f]+$"))
    app.add_handler(CallbackQueryHandler(cb_drop, pattern=r"^drop:[0-9a-f]+$"))
    app.add_handler(CallbackQueryHandler(cb_inbox, pattern=r"^in:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_job, pattern=r"^cancel:\d+$"))

    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.MimeType("audio/mpeg") |
                                   filters.Document.MimeType("audio/wav") | filters.Document.MimeType("audio/x-wav"),
                                   on_audio))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^https?://\S+"), on_link))
    app.add_error_handler(on_error)
    return app
