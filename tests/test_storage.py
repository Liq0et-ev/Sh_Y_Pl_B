from shorts_factory.config import PipelineOptions
from shorts_factory.storage import Storage


async def make_storage(tmp_path) -> Storage:
    s = Storage(str(tmp_path / "t.sqlite3"))
    await s.connect()
    return s


async def test_options_default_then_saved(tmp_path):
    s = await make_storage(tmp_path)
    assert await s.get_options(1) == PipelineOptions()
    await s.save_options(1, PipelineOptions(mode="slice", music=False))
    await s.save_options(1, PipelineOptions(mode="slice", music=False, subtitles=False))  # upsert
    got = await s.get_options(1)
    assert got.mode == "slice" and got.subtitles is False
    assert await s.get_options(2) == PipelineOptions()   # other chats unaffected
    await s.close()


async def test_job_lifecycle_and_chat_ownership(tmp_path):
    s = await make_storage(tmp_path)
    jid = await s.add_job(10, "/v.mp4", PipelineOptions())
    job = await s.get_job(jid)
    assert job.status == "queued" and job.outputs == []
    assert await s.get_job(jid, chat_id=99) is None       # someone else's job is invisible
    await s.set_status(jid, "done", outputs=["/o1.mp4", "/o2.mp4"])
    job = await s.get_job(jid, chat_id=10)
    assert job.status == "done" and job.outputs == ["/o1.mp4", "/o2.mp4"]
    await s.set_status(jid, "failed", error="boom")       # outputs preserved when not given
    assert (await s.get_job(jid)).outputs == ["/o1.mp4", "/o2.mp4"]
    await s.close()


async def test_recent_jobs_order_and_limit(tmp_path):
    s = await make_storage(tmp_path)
    ids = [await s.add_job(1, f"/{i}.mp4", PipelineOptions()) for i in range(7)]
    await s.add_job(2, "/other.mp4", PipelineOptions())
    recent = await s.recent_jobs(1, limit=5)
    assert [j.id for j in recent] == ids[::-1][:5]
    await s.close()


async def test_active_jobs_marked_interrupted_after_restart(tmp_path):
    s = await make_storage(tmp_path)
    a = await s.add_job(1, "/a.mp4", PipelineOptions())
    b = await s.add_job(1, "/b.mp4", PipelineOptions())
    c = await s.add_job(1, "/c.mp4", PipelineOptions())
    await s.set_status(b, "running")
    await s.set_status(c, "done")
    await s.close()
    s2 = await make_storage(tmp_path)                     # simulated restart
    assert (await s2.get_job(a)).status == "interrupted"
    assert (await s2.get_job(b)).status == "interrupted"
    assert (await s2.get_job(c)).status == "done"
    await s2.close()
