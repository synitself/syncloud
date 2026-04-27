import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _require_env(name: str) -> str:
	value = os.getenv(name)
	if not value:
		raise RuntimeError(f"Missing required environment variable: {name}")
	return value


TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
DOWNLOAD_FOLDER = os.getenv("DOWNLOAD_FOLDER", "downloads")
BOT_VERSION = os.getenv("BOT_VERSION", "1.1.0")

API_HASH = _require_env("API_HASH")
try:
	API_ID = int(_require_env("API_ID"))
except ValueError as exc:
	raise RuntimeError("Environment variable API_ID must be an integer") from exc
