"""Environment-based app config (bot/infra) and per-job pipeline options."""
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav"}

SHORTS_WIDTH = 1080
SHORTS_HEIGHT = 1920
# Pixel margins that avoid the YouTube Shorts UI overlay (like/share/subscribe)
SAFE_ZONE_LEFT = 60
SAFE_ZONE_RIGHT = 60


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit())


@dataclass(frozen=True)
class AppConfig:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    data_dir: Path
    inbox_dir: Path
    music_dir: Path
    work_dir: Path
    output_dir: Path
    db_path: str
    api_base_url: str | None
    whisper_model: str
    max_upload_mb: int


def load_config() -> AppConfig:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Create a .env file (see .env.example) "
            "with a token obtained from @BotFather."
        )
    data = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
    cfg = AppConfig(
        telegram_token=token,
        allowed_user_ids=_ids(os.environ.get("ALLOWED_USER_IDS", "")),
        data_dir=data,
        inbox_dir=Path(os.environ.get("INBOX_DIR", data / "inbox")).resolve(),
        music_dir=Path(os.environ.get("MUSIC_DIR", data / "music")).resolve(),
        work_dir=Path(os.environ.get("WORK_DIR", data / "work")).resolve(),
        output_dir=Path(os.environ.get("OUTPUT_DIR", data / "output")).resolve(),
        db_path=os.environ.get("DB_PATH", str(data / "shorts_factory.sqlite3")),
        api_base_url=os.environ.get("TELEGRAM_API_BASE_URL") or None,
        whisper_model=os.environ.get("WHISPER_MODEL", "medium"),
        max_upload_mb=int(os.environ.get("TELEGRAM_UPLOAD_LIMIT_MB", "50")),
    )
    for d in (cfg.data_dir, cfg.inbox_dir, cfg.music_dir, cfg.work_dir, cfg.output_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


@dataclass
class PipelineOptions:
    """Everything a single job can tweak. Stored per chat as JSON."""

    # "highlight": one clip built from the most dynamic moments (Dinamic_Control)
    # "slice":     whole video cut into sequential random-length clips (One_Minute_Videos)
    mode: str = "highlight"
    # highlight mode
    variant: str = "heuristic"      # heuristic | surprisal | attention
    target_sec: float = 60.0
    threshold_k: float = 1.5
    buffer_sec: float = 5.0
    merge_gap_sec: float = 3.0
    # slice mode
    min_clip_sec: float = 45.0
    max_clip_sec: float = 60.0
    max_clips: int = 10              # safety cap per job
    # formatting
    vertical: str = "blur"           # blur (full frame over blurred bg) | crop (fill 9:16) | off
    # subtitles
    subtitles: bool = True
    language: str | None = None      # None = auto-detect (en/ru)
    brand_kit: str = "viral_yellow"
    # music
    music: bool = True
    music_volume: float = 0.2

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "PipelineOptions":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def validate(self) -> None:
        if self.mode not in ("highlight", "slice"):
            raise ValueError(f"Unknown mode: {self.mode}")
        if self.variant not in ("heuristic", "surprisal", "attention"):
            raise ValueError(f"Unknown variant: {self.variant}")
        if self.vertical not in ("blur", "crop", "off"):
            raise ValueError(f"Unknown vertical mode: {self.vertical}")
        if not 0.0 <= self.music_volume <= 1.0:
            raise ValueError("music_volume must be within 0..1")
        if self.min_clip_sec >= self.max_clip_sec:
            raise ValueError("min_clip_sec must be less than max_clip_sec")
