"""Central configuration for the local leads pipeline.

Environment variables are loaded from ``.env`` via python-dotenv.
See ``.env.example`` for the full list of available variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()

VERSION = "1.0.0"

# ── API sync (optional) ───────────────────────────────────────────────────────

API_URL = os.getenv("API_URL", "")
API_KEY = os.getenv("API_KEY", "")

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

HEADLESS = True

# Delays (seconds) between browser actions to avoid bot detection
DELAY_INITIAL_LOAD = 3.0
DELAY_AFTER_CONSENT = 3.0
DELAY_PER_CARD_CLICK = 4.0
DELAY_SCROLL_AFTER_EXTRACT = 2.0
JITTER_RANGE = 0.2          # applied as ±(base × JITTER_RANGE) random variance
RETRY_BACKOFF_BASE = 2      # exponential backoff base on timeout retries
MAX_EXTRACTION_RETRIES = 3

# ── AI message generation ─────────────────────────────────────────────────────

SENDER_NAME = os.getenv("SENDER_NAME")
SENDER_COMPANY = os.getenv("SENDER_COMPANY")
LLM_MODEL = os.getenv("LLM_MODEL")
