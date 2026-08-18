"""Environment-driven config with sensible defaults."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    anthropic_base_url: str | None
    model: str
    whisper_model: str
    hotkey: str
    input_device: str | None  # int as str (device index) OR substring of device name
    tts_enabled: bool
    voice_rate: int | None

    @classmethod
    def from_env(cls) -> "Config":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None
        return cls(
            anthropic_api_key=key,
            anthropic_base_url=base_url,
            model=os.environ.get("VOXIE_MODEL", "claude-sonnet-4-5").strip(),
            whisper_model=os.environ.get("VOXIE_WHISPER_MODEL", "base.en").strip(),
            hotkey=os.environ.get("VOXIE_HOTKEY", "<ctrl>+<alt>+<space>").strip(),
            input_device=(os.environ.get("VOXIE_INPUT_DEVICE", "").strip() or None),
            tts_enabled=os.environ.get("VOXIE_TTS", "on").strip().lower() not in ("off", "0", "false", "no"),
            voice_rate=_int_or_none(os.environ.get("VOXIE_VOICE_RATE", "").strip()),
        )


def _int_or_none(s: str) -> int | None:
    try:
        return int(s) if s else None
    except ValueError:
        return None
