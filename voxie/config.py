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
        )
