"""Pipeline orchestrator: scrape → analyze → generate → send.

Runs the three phases in sequence and persists the final leads via the API
client. Typically invoked by the Streamlit dashboard, but also usable directly
from the command line.
"""

import argparse
import os

from config import HEADLESS, SOCIAL_DOMAINS
from scraper.maps_scraper import scrape
from scraper.web_analyzer import analyze
from ai.message_generator import generate
from api.client import send

# All fields that constitute a reachable contact channel
_CONTACT_FIELDS = ("email", "phone", *SOCIAL_DOMAINS)


def _is_contactable(lead: dict) -> bool:
    return any(lead.get(f, "").strip() for f in _CONTACT_FIELDS)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local leads automation pipeline")
    parser.add_argument("--profession", required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument("--max", type=int, default=None, dest="max_results",
                        metavar="N", help="Max listings to collect (default: all)")
    parser.add_argument("--no-headless", action="store_true", dest="no_headless",
                        help="Open the browser visibly (for debugging)")
    return parser.parse_args()


def main():
    args = _parse_args()
    headless = HEADLESS and not args.no_headless

    os.makedirs("data", exist_ok=True)

    # Phase 1 — scrape Google Maps
    leads = scrape(args.profession, args.city, headless, args.max_results)
    print(f"[+] Phase 1 complete: {len(leads)} leads scraped")

    # Phase 2 — analyze each website
    print("[+] Phase 2: analyzing websites...")
    analyzed = []
    for i, lead in enumerate(leads, 1):
        result = analyze(lead)
        analyzed.append(result)
        cms = result["cms"] or "—"
        score = result["seo_score"]
        print(f"  [{i}/{len(leads)}] {lead['lead']} | {cms} | score: {score}")

    # Phase 3 — generate outreach messages for contactable leads only
    contactable = [l for l in analyzed if _is_contactable(l)]
    skipped = len(analyzed) - len(contactable)
    print(f"[+] Phase 3: {len(contactable)} contactable leads ({skipped} skipped — no contact info)")

    for i, lead in enumerate(contactable, 1):
        msg = generate(lead)
        lead["subject"] = msg["subject"]
        lead["body"] = msg["body"]
        print(f"  [{i}/{len(contactable)}] {lead['lead']}")

    send(analyzed)


if __name__ == "__main__":
    main()
