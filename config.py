"""Central configuration for the local leads pipeline.

Environment variables are loaded from ``.env`` via python-dotenv.
See ``.env.example`` for the full list of available variables.
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ── API worker (SegurSEO-API job queue) ───────────────────────────────────────

API_BASE_URL = os.getenv("API_BASE_URL", "")
API_TOKEN = os.getenv("API_TOKEN", "")
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

# ── AI message generation ─────────────────────────────────────────────────────

SENDER_COMPANY = os.getenv("SENDER_COMPANY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat")
