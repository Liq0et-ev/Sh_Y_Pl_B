import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


from shorts_factory import bot, jobs
from shorts_factory.config import AppConfig, PipelineOptions
from shorts_factory.pipeline import Cancelled, PipelineResult
from shorts_factory.storage import Storage


def make_cfg(tmp_path, allowed=(1,), api=None) -> AppConfig:
    dirs = {n: tmp_path / n for n in ("data", "inbox", "music", "work", "output")}
    for d in dirs.values():
        d.mkdir(exist_ok=True)
    return AppConfig(
        telegram_token="1:x", allowed_user_ids=frozenset(allowed), data_dir=dirs["data"],
        inbox_dir=dirs["inbox"], music_dir=dirs["music"], work_dir=dirs["work"], output_dir=dirs["output"],
        db_path=":memory:", api_base_url=api, whisper_model="tiny", max_upload_mb=50,
    )


# ---------------------------------------------------------------- panel logic
def test_apply_option_toggles_and_cycles():
    o = PipelineOptions()
    bot.apply_option(o, "mode");       assert o.mode == "slice"
    bot.apply_option(o, "mode");       assert o.mode == "highlight"
    bot.apply_option(o, "subtitles");  assert o.subtitles is False
    bot.apply_option(o, "music");      assert o.music is False
    bot.apply_option(o, "vertical");   assert o.vertical == "crop"
    bot.apply_option(o, "vertical");   assert o.vertical == "off"
    bot.apply_option(o, "vertical");   assert o.vertical == "blur"
    bot.apply_option(o, "variant");    assert o.variant == "surprisal"
    kits = set()
    for _ in range(len(bot.KITS)):
        bot.apply_option(o, "kit"); kits.add(o.brand_kit)
    assert kits == set(bot.KITS)
    o.validate()


def test_length_buttons_stay_within_shorts_limits():
    o = PipelineOptions()
    for _ in range(40):
        bot.apply_option(o, "len+")
    assert o.target_sec == 180
    for _ in range(40):
        bot.apply_option(o, "len-")
    assert o.target_sec == 15
    o.mode = "slice"
    for _ in range(40):
        bot.apply_option(o, "len+")
        o.validate()
    assert o.max_clip_sec == 180
    for _ in range(40):
        bot.apply_option(o, "len-")
        o.validate()                       # min must always stay below max
    assert o.max_clip_sec >= 30 and o.min_clip_sec < o.max_clip_sec


def test_every_panel_callback_matches_registered_pattern(tmp_path):
    app = bot.build_application(make_cfg(tmp_path), Storage(":memory:"))
    patterns = [h.pattern for hs in app.handlers.values() for h in hs if hasattr(h, "pattern") and h.pattern]
    kb = bot.panel_keyboard(PipelineOptions(), "ab12cd")
    for row in kb.inline_keyboard:
        for b in row:
            assert len(b.callback_data.encode()) <= 64
            assert any(p.match(b.callback_data) for p in patterns), b.callback_data
    kb = bot.panel_keyboard(PipelineOptions(), "-")
    assert all(not b.callback_data.startswith(("go:", "drop:")) for row in kb.inline_keyboard for b in row)


def test_panel_text_mentions_mode_specific_info():
    assert "Длина хайлайта" in bot.panel_text(PipelineOptions(mode="highlight"), "x.mp4")
    assert "Длина клипа" in bot.panel_text(PipelineOptions(mode="slice"), "x.mp4")


# ---------------------------------------------------------------- access control
def _update(user_id=1, text="/start", callback_data=None):
    msg = MagicMock(); msg.reply_text = AsyncMock(); msg.text = text
    query = None
    if callback_data:
        query = MagicMock(); query.answer = AsyncMock(); query.data = callback_data
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=msg,
                           message=msg, callback_query=query, effective_chat=SimpleNamespace(id=user_id))


def _ctx(cfg, **extra):
    return SimpleNamespace(bot_data={"cfg": cfg, **extra}, user_data={}, bot=MagicMock())


async def test_unauthorised_user_is_rejected_everywhere(tmp_path):
    cfg = make_cfg(tmp_path, allowed=(1,))
    upd = _update(user_id=999)
    await bot.cmd_jobs(upd, _ctx(cfg))
    upd.effective_message.reply_text.assert_awaited_once()
    assert "Нет доступа" in upd.effective_message.reply_text.await_args.args[0]
    cb = _update(user_id=999, callback_data="go:abc123")
    await bot.cb_go(cb, _ctx(cfg))
    cb.callback_query.answer.assert_awaited_once()


async def test_start_shows_id_to_unlisted_user_and_help_to_allowed(tmp_path):
    cfg = make_cfg(tmp_path, allowed=(1,))
    stranger = _update(user_id=555)
    await bot.cmd_start(stranger, _ctx(cfg))
    assert "555" in stranger.message.reply_text.await_args.args[0]
    owner = _update(user_id=1)
    await bot.cmd_start(owner, _ctx(cfg))
    assert owner.message.reply_text.await_args.args[0] == bot.HELP_TEXT


async def test_empty_allowlist_fails_closed(tmp_path):
    cfg = make_cfg(tmp_path, allowed=())
    upd = _update(user_id=1)
    await bot.cmd_music(upd, _ctx(cfg))
    assert "Нет доступа" in upd.message.reply_text.await_args.args[0]


# ---------------------------------------------------------------- uploads
async def test_oversized_video_is_refused_with_inbox_hint(tmp_path):
    cfg = make_cfg(tmp_path)
    upd = _update()
    upd.effective_message.video = SimpleNamespace(file_name="big.mp4", file_size=80 * 1024 * 1024, file_id="f")
    upd.effective_message.document = None
    await bot.on_video(upd, _ctx(cfg))
    text = upd.effective_message.reply_text.await_args.args[0]
    assert "20 МБ" in text and str(cfg.inbox_dir) in text


async def test_non_video_document_is_refused(tmp_path):
    cfg = make_cfg(tmp_path)
    upd = _update()
    upd.effective_message.video = None
    upd.effective_message.document = SimpleNamespace(file_name="notes.pdf", file_size=10, file_id="f")
    await bot.on_video(upd, _ctx(cfg))
    assert "не похоже на видеофайл" in upd.effective_message.reply_text.await_args.args[0]


async def test_audio_upload_rejects_path_traversal_and_bad_ext(tmp_path):
    cfg = make_cfg(tmp_path)
    tg_file = SimpleNamespace(download_to_drive=AsyncMock())
    ctx = _ctx(cfg); ctx.bot.get_file = AsyncMock(return_value=tg_file)
    upd = _update()
    upd.effective_message.audio = None
    upd.effective_message.document = SimpleNamespace(file_name="../../evil.mp3", file_size=10, file_id="f")
    await bot.on_audio(upd, ctx)
    dest = tg_file.download_to_drive.await_args.args[0]
    assert Path(dest).parent == cfg.music_dir and Path(dest).name == "evil.mp3"
    upd.effective_message.document = SimpleNamespace(file_name="x.exe", file_size=10, file_id="f")
    await bot.on_audio(upd, ctx)
    assert tg_file.download_to_drive.await_count == 1


# ---------------------------------------------------------------- panel -> job
async def test_option_then_go_queues_job_with_chosen_options(tmp_path):
    cfg = make_cfg(tmp_path)
    storage = Storage(str(tmp_path / "s.sqlite3")); await storage.connect()
    submitted = {}
    fake_jobs = SimpleNamespace(submit=AsyncMock(side_effect=lambda c, p, o: submitted.update(o=o, p=p) or (7, 1)))
    ctx = _ctx(cfg, storage=storage, jobs=fake_jobs)
    ctx.user_data["pending"] = {"ab12cd": {"path": str(tmp_path / "v.mp4"), "opts": PipelineOptions().to_dict(), "label": "v"}}

    def cb(data):
        u = _update(callback_data=data)
        u.callback_query.message = SimpleNamespace(chat_id=1, reply_text=AsyncMock())
        u.callback_query.edit_message_text = AsyncMock()
        return u

    await bot.cb_option(cb("opt:ab12cd:mode"), ctx)
    await bot.cb_option(cb("opt:ab12cd:music"), ctx)
    go = cb("go:ab12cd")
    await bot.cb_go(go, ctx)
    assert submitted["o"].mode == "slice" and submitted["o"].music is False
    assert "ab12cd" not in ctx.user_data["pending"]           # one-shot token
    assert (await storage.get_options(1)).mode == "slice"     # remembered as new default
    again = cb("go:ab12cd")
    await bot.cb_go(again, ctx)                               # replay is harmless
    assert "устарело" in again.callback_query.edit_message_text.await_args.args[0]
    assert fake_jobs.submit.await_count == 1
    await storage.close()


# ---------------------------------------------------------------- job manager
class FakeBot:
    def __init__(self):
        self.sent, self.videos, self.docs = [], [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(edit_text=AsyncMock())

    async def send_video(self, chat_id, fh, **kw):
        self.videos.append(kw.get("caption"))

    async def send_document(self, chat_id, fh, **kw):
        self.docs.append(kw.get("caption"))


async def _manager(tmp_path, monkeypatch, fake_run):
    cfg = make_cfg(tmp_path)
    storage = Storage(str(tmp_path / "j.sqlite3")); await storage.connect()
    fb = FakeBot()
    monkeypatch.setattr(jobs, "run_pipeline", fake_run)
    mgr = jobs.JobManager(cfg, storage, fb)
    worker = asyncio.create_task(mgr.run_forever())
    return cfg, storage, fb, mgr, worker


async def _wait_status(storage, job_id, wanted, timeout=5):
    for _ in range(int(timeout / 0.02)):
        if (await storage.get_job(job_id)).status in wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} stuck at {(await storage.get_job(job_id)).status}")


async def test_successful_job_delivers_video_and_removes_own_upload(tmp_path, monkeypatch):
    out = tmp_path / "result.mp4"; out.write_bytes(b"x" * 100)

    def fake_run(source, opts, *a, **k):
        return PipelineResult(outputs=[out], notes=["hello note"])

    cfg, storage, fb, mgr, worker = await _manager(tmp_path, monkeypatch, fake_run)
    uploads = cfg.work_dir / "uploads"; uploads.mkdir()
    src = uploads / "1_5.mp4"; src.write_bytes(b"v")
    inbox_file = cfg.inbox_dir / "keep.mp4"; inbox_file.write_bytes(b"v")
    j1, pos = await mgr.submit(1, src, PipelineOptions())
    j2, _ = await mgr.submit(1, inbox_file, PipelineOptions())
    assert pos == 1
    await _wait_status(storage, j1, {"done"}); await _wait_status(storage, j2, {"done"})
    assert fb.videos == ["result.mp4", "result.mp4"]
    assert any("hello note" in m for m in fb.sent)
    assert not src.exists()                  # bot-downloaded upload cleaned up
    assert inbox_file.exists()               # user's inbox file never deleted
    worker.cancel(); await storage.close()


async def test_failed_job_reports_error_and_worker_survives(tmp_path, monkeypatch):
    calls = []

    def fake_run(source, *a, **k):
        calls.append(source)
        if len(calls) == 1:
            raise RuntimeError("ffmpeg exploded")
        return PipelineResult(outputs=[])

    cfg, storage, fb, mgr, worker = await _manager(tmp_path, monkeypatch, fake_run)
    bad, _ = await mgr.submit(1, tmp_path / "a.mp4", PipelineOptions())
    good, _ = await mgr.submit(1, tmp_path / "b.mp4", PipelineOptions())
    await _wait_status(storage, bad, {"failed"}); await _wait_status(storage, good, {"done"})
    assert "ffmpeg exploded" in (await storage.get_job(bad)).error
    worker.cancel(); await storage.close()


async def test_cancel_running_and_queued_jobs(tmp_path, monkeypatch):
    started = asyncio.Event()
    gate = __import__("threading").Event()

    def fake_run(source, opts, out, work, music, model, progress, should_cancel):
        started.set()
        for _ in range(500):
            if should_cancel():
                raise Cancelled()
            gate.wait(0.01)
        return PipelineResult()

    cfg, storage, fb, mgr, worker = await _manager(tmp_path, monkeypatch, fake_run)
    running, _ = await mgr.submit(1, tmp_path / "a.mp4", PipelineOptions())
    queued, pos = await mgr.submit(1, tmp_path / "b.mp4", PipelineOptions())
    assert pos == 2
    await asyncio.wait_for(started.wait(), 3)
    assert await mgr.cancel(queued, chat_id=1) is True
    assert await mgr.cancel(running, chat_id=2) is False      # not your job
    assert await mgr.cancel(running, chat_id=1) is True
    await _wait_status(storage, running, {"cancelled"})
    await asyncio.sleep(0.1)
    assert (await storage.get_job(queued)).status == "cancelled"
    assert len([1 for _ in fb.videos]) == 0
    assert await mgr.cancel(running, chat_id=1) is False      # already finished
    worker.cancel(); await storage.close()


async def test_oversized_output_is_not_uploaded_but_path_is_reported(tmp_path, monkeypatch):
    big = tmp_path / "big.mp4"
    with open(big, "wb") as f:
        f.truncate(51 * 1024 * 1024)

    cfg, storage, fb, mgr, worker = await _manager(tmp_path, monkeypatch,
                                                   lambda *a, **k: PipelineResult(outputs=[big]))
    jid, _ = await mgr.submit(1, tmp_path / "a.mp4", PipelineOptions())
    await _wait_status(storage, jid, {"done"}); await asyncio.sleep(0.1)
    assert fb.videos == []
    assert any("big.mp4" in m and "слишком большой" in m for m in fb.sent)
    worker.cancel(); await storage.close()
