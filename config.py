"""Central configuration for the local leads pipeline.

Environment variables are loaded from ``.env`` via python-dotenv.
See ``.env.example`` for the full list of available variables.
"""

import os
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(override=True)

# The worker's own version, tracked independently of the API's and the theme's — each
# project versions on its own cycle, and this is simply the analogous place to keep it
# (composer.json there, style.css in the theme). The three numbers are not meant to
# match. Kept here rather than in packaging metadata because the worker is run directly
# (``python worker.py``) rather than installed, so this is the only place a running
# process can read its own version from. Tagged v<APP_VERSION> at release time.
APP_VERSION = "2.1.1"

# ── API worker (SegurSEO-API job queue) ───────────────────────────────────────

# Trailing slash stripped: every endpoint is built as f"{API_BASE_URL}/api/...", so a
# pasted URL ending in "/" would produce a double slash on every request.
API_BASE_URL = os.getenv("API_BASE_URL", "").rstrip("/")
API_TOKEN = os.getenv("API_TOKEN", "")
if not API_BASE_URL or not API_TOKEN:
    raise EnvironmentError("API_BASE_URL and API_TOKEN must be set in .env")

# The bearer token goes on every request, so plain HTTP would put it on the wire in
# cleartext. Tolerated only against a local API, where the traffic never leaves the
# machine. The mistake this catches is the likely one: copying .env.example, changing
# the host for the production one, and leaving its http:// scheme in place.
if not API_BASE_URL.startswith("https://") and (
    urlparse(API_BASE_URL).hostname not in ("localhost", "127.0.0.1", "::1")
):
    raise EnvironmentError(
        f"API_BASE_URL must use https:// for a remote host (got {API_BASE_URL!r}); "
        "over plain HTTP the worker token would travel in cleartext"
    )
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))

# ── Scraper ───────────────────────────────────────────────────────────────────

# Maps platform name → root domain, used for detection and contact field naming
SOCIAL_DOMAINS = {
    "instagram": "instagram.com",
    "facebook": "facebook.com",
    "youtube": "youtube.com",
    "linkedin": "linkedin.com",
    "twitter": "twitter.com",
    "tiktok": "tiktok.com",
}

# Delays (seconds) between browser actions to avoid bot detection
DELAY_INITIAL_LOAD = 3.0
DELAY_AFTER_CONSENT = 3.0
DELAY_PER_CARD_CLICK = 4.0
DELAY_SCROLL_AFTER_EXTRACT = 2.0
JITTER_RANGE = 0.2          # applied as ±(base × JITTER_RANGE) random variance
RETRY_BACKOFF_BASE = 2      # exponential backoff base on timeout retries
MAX_EXTRACTION_RETRIES = 3
MAX_IDLE_SCROLLS = 5        # consecutive scroll waves with no new non-skipped lead before giving up
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

# ── AI message generation ─────────────────────────────────────────────────────

SENDER_COMPANY = os.getenv("SENDER_COMPANY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat")
