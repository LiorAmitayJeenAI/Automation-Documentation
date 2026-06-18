import os
from pathlib import Path
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


AZURE_OPENAI_API_KEY = _require("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = _require("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.5")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")

CONFLUENCE_EMAIL = _require("CONFLUENCE_EMAIL")
CONFLUENCE_API_TOKEN = _require("CONFLUENCE_API_TOKEN")
CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "https://jeenai.atlassian.net")

GAMMA_API_KEY = _require("GAMMA_API_KEY")

SP_TENANT_ID = _require("SP_TENANT_ID")
SP_CLIENT_ID = _require("SP_CLIENT_ID")
SP_SITE_URL = os.getenv("SP_SITE_URL", "https://jeenai365.sharepoint.com/sites/JEEN.AI")
SP_FOLDER_PATH = os.getenv("SP_FOLDER_PATH", "Testing/LiorAmitay/JeenTutorial")
SP_SCREENSHOTS_FOLDER = os.getenv("SP_SCREENSHOTS_FOLDER", "Testing/LiorAmitay")
# Base folder under which "Save to SharePoint" creates new timestamped export
# folders. This path is relative to the SharePoint document library root.
SP_EXPORT_BASE = os.getenv(
    "SP_EXPORT_BASE",
    "jeen tutorial/tutorial automation/automation output",
)

JEEN_USERNAME = os.getenv("JEEN_USERNAME", "")
JEEN_PASSWORD = os.getenv("JEEN_PASSWORD", "")

REGULAR_URL = os.getenv("REGULAR_URL", "https://jeenai.app")
ADMIN_URL = os.getenv("ADMIN_URL", "https://admin.jeenai.app")

# Public base URL of this backend (e.g. an ngrok/cloudflared tunnel or deployed
# host). When set, screenshots are served to Gamma via the backend proxy route
# (/screenshots/...) so the image URL ends in a recognized image extension.
# Leave empty to pass the SharePoint download URL to Gamma directly.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Optional imgbb API key for uploading screenshots to a public image host.
# When set, screenshots get clean public URLs (https://i.ibb.co/…/image.png)
# that Gamma can reliably fetch — no tunnel required.
# Free key at https://api.imgbb.com/
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "")

SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", str(Path.home() / ".langflow" / "data"))

VIDEO_DIR = os.getenv("VIDEO_DIR", str(Path.home() / ".langflow" / "videos"))
VIDEO_PROJECT_DIR = os.getenv(
    "VIDEO_PROJECT_DIR",
    str(Path(__file__).resolve().parent.parent / "video"),
)
JEEN_VIDEOS_DIR = os.getenv(
    "JEEN_VIDEOS_DIR",
    str(Path(__file__).resolve().parent.parent / "JeenVideos"),
)

BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8000"))

# TTS — ElevenLabs (primary) or Azure Speech (fallback). Leave blank to skip audio.
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # Sarah
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "swedencentral")
