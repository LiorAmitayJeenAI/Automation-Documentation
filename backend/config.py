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
SP_DRAFT_PDF_FOLDER = os.getenv("SP_DRAFT_PDF_FOLDER", "jeen tutorial/Tutorial Automation/Draft/Presentation Draft")
SP_DRAFT_VIDEO_FOLDER = os.getenv("SP_DRAFT_VIDEO_FOLDER", "jeen tutorial/Tutorial Automation/Draft/Video Draft")
SP_SCREENSHOTS_FOLDER = os.getenv("SP_SCREENSHOTS_FOLDER", "Testing/LiorAmitay")
# Base folder under which "Save to SharePoint" creates date-based export
# subfolders (e.g. "Presentations 24-06-2026", "Videos 24-06-2026").
SP_EXPORT_BASE = os.getenv(
    "SP_EXPORT_BASE",
    "jeen tutorial/Tutorial Automation/Automation Output",
)
# Folder where the summary Excel file is uploaded on "Save to SharePoint".
SP_EXCEL_EXPORT_PATH = os.getenv(
    "SP_EXCEL_EXPORT_PATH",
    "jeen tutorial/Tutorial Automation",
)

JEEN_USERNAME = os.getenv("JEEN_USERNAME", "")
JEEN_PASSWORD = os.getenv("JEEN_PASSWORD", "")

JEEN_USERNAME_HE = os.getenv("JEEN_USERNAME_HE", JEEN_USERNAME)
JEEN_PASSWORD_HE = os.getenv("JEEN_PASSWORD_HE", JEEN_PASSWORD)
JEEN_USERNAME_EN = os.getenv("JEEN_USERNAME_EN", "")
JEEN_PASSWORD_EN = os.getenv("JEEN_PASSWORD_EN", "")
JEEN_USERNAME_FINOPS = os.getenv("JEEN_USERNAME_FINOPS", "")
JEEN_PASSWORD_FINOPS = os.getenv("JEEN_PASSWORD_FINOPS", "")


def get_jeen_credentials(language: str = "he", link_type: str = "regular") -> tuple[str, str]:
    """Return (username, password) for the Jeen account matching *link_type* / *language*."""
    if link_type == "finops" and JEEN_USERNAME_FINOPS:
        return JEEN_USERNAME_FINOPS, JEEN_PASSWORD_FINOPS
    if language == "en" and JEEN_USERNAME_EN:
        return JEEN_USERNAME_EN, JEEN_PASSWORD_EN
    return JEEN_USERNAME_HE, JEEN_PASSWORD_HE

REGULAR_URL = os.getenv("REGULAR_URL", "https://jeenai.app")
ADMIN_URL = os.getenv("ADMIN_URL", "https://admin.jeenai.app")
FINOPS_URL = os.getenv("FINOPS_URL", "https://finops.jeenai.app")

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

# Clickable-element capture tuning (route_crawler). MAX_CLICKABLE_ELEMENTS caps
# how many TOP-LEVEL buttons are persisted per route (nested child buttons under
# an opener do NOT count against this cap). The cap is applied AFTER the buttons
# are scored/prioritized in _gather_page_info (page-content + opener + has-text
# buttons rank highest), and openers that reveal child menus are always kept
# first, so the most meaningful buttons survive truncation. It exists only to
# bound routes_map.json size and the LLM prompt length — raise it via env if a
# button-rich route is losing real, page-specific actions. The child-button pass
# clicks safe "opener" buttons (dropdowns/menus/modals) and records the buttons
# they reveal as nested `opens`; it is opt-out via CAPTURE_CHILD_BUTTONS and
# bounded by the per-opener / per-page caps to keep crawl time reasonable.
MAX_CLICKABLE_ELEMENTS = int(os.getenv("MAX_CLICKABLE_ELEMENTS", "30"))
CAPTURE_CHILD_BUTTONS = os.getenv("CAPTURE_CHILD_BUTTONS", "true").lower() == "true"
MAX_OPENERS_PER_PAGE = int(os.getenv("MAX_OPENERS_PER_PAGE", "6"))
MAX_CHILDREN_PER_OPENER = int(os.getenv("MAX_CHILDREN_PER_OPENER", "8"))

# Per-page timeout for the crawler. If a single page's processing (navigation +
# opener capture) exceeds this budget, the crawler skips it and moves on.
CRAWLER_PAGE_TIMEOUT_S = int(os.getenv("CRAWLER_PAGE_TIMEOUT_S", "120"))

# TTS — ElevenLabs (primary) or Azure Speech (fallback). Leave blank to skip audio.
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # Sarah
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "swedencentral")
