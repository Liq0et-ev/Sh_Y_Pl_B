"""Command-line entry point: run the same pipeline without Telegram.

    python -m shorts_factory.cli video.mp4 --mode slice --no-music
"""
import argparse
import sys
from pathlib import Path

from .config import ROOT, PipelineOptions
from .pipeline import run_pipeline
from .subtitles.brand_kits import PRESETS


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Convert a long video into YouTube Shorts.")
    p.add_argument("video")
    p.add_argument("--mode", choices=["highlight", "slice"], default="highlight")
    p.add_argument("--variant", choices=["heuristic", "surprisal", "attention"], default="heuristic")
    p.add_argument("--target", type=float, default=60.0, help="highlight length in seconds")
    p.add_argument("--threshold", type=float, default=1.5)
    p.add_argument("--min", type=float, default=45.0, dest="min_clip")
    p.add_argument("--max", type=float, default=60.0, dest="max_clip")
    p.add_argument("--max-clips", type=int, default=10)
    p.add_argument("--vertical", choices=["blur", "crop", "off"], default="blur")
    p.add_argument("--kit", choices=sorted(PRESETS), default="viral_yellow")
    p.add_argument("--lang", choices=["en", "ru"], default=None)
    p.add_argument("--whisper", default="medium")
    p.add_argument("--no-subtitles", action="store_true")
    p.add_argument("--no-music", action="store_true")
    p.add_argument("--music-volume", type=float, default=0.2)
    p.add_argument("--music-dir", default=str(ROOT / "data" / "music"))
    p.add_argument("--output-dir", default=str(ROOT / "data" / "output"))
    a = p.parse_args(argv)

    opts = PipelineOptions(
        mode=a.mode, variant=a.variant, target_sec=a.target, threshold_k=a.threshold,
        min_clip_sec=a.min_clip, max_clip_sec=a.max_clip, max_clips=a.max_clips,
        vertical=a.vertical, subtitles=not a.no_subtitles, language=a.lang,
        brand_kit=a.kit, music=not a.no_music, music_volume=a.music_volume,
    )
    res = run_pipeline(
        Path(a.video), opts, Path(a.output_dir), ROOT / "data" / "work", Path(a.music_dir),
        whisper_model=a.whisper, progress=lambda pct, msg: print(f"[{pct:5.1f}%] {msg}"),
    )
    for note in res.notes:
        print("note:", note)
    for out in res.outputs:
        print("output:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
