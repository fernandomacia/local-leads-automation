import os
import pandas as pd

from config import DEFAULT_PROFESSION, DEFAULT_CITY, HEADLESS
from scraper.maps_scraper import scrape
from scraper.web_analyzer import analyze
from ai.message_generator import generate

# zip_code must stay as string to preserve leading zeros (e.g. "03330")
_CSV_DTYPES = {"zip_code": str}

# Channels we can use for outreach (YouTube excluded — no DM capability)
_CONTACT_FIELDS = ("email", "instagram", "facebook", "linkedin", "twitter", "tiktok", "phone")


def _save(leads: list[dict], path: str) -> None:
    pd.DataFrame(leads).to_csv(path, index=False)


def _is_contactable(lead: dict) -> bool:
    return any(lead.get(f, "").strip() for f in _CONTACT_FIELDS)


def main():
    os.makedirs("data", exist_ok=True)

    # Phase 1 — scrape Google Maps
    leads = scrape(DEFAULT_PROFESSION, DEFAULT_CITY, HEADLESS)
    _save(leads, "data/leads.csv")
    print(f"[+] Phase 1 complete: {len(leads)} leads → data/leads.csv")

    # Phase 2 — analyze each website
    print("[+] Phase 2: analyzing websites...")
    analyzed = []
    for i, lead in enumerate(leads, 1):
        result = analyze(lead)
        analyzed.append(result)
        cms = result["cms"] or "—"
        score = result["seo_score"]
        print(f"  [{i}/{len(leads)}] {lead['name']} | {cms} | score: {score}")

    _save(analyzed, "data/leads.csv")
    print(f"[+] Phase 2 complete: {len(analyzed)} leads analyzed → data/leads.csv")

    # Phase 3 — filter contactable leads, generate outreach messages
    contactable = [l for l in analyzed if _is_contactable(l)]
    skipped = len(analyzed) - len(contactable)
    print(f"[+] Phase 3: {len(contactable)} contactable leads ({skipped} skipped — no contact info)")

    for i, lead in enumerate(contactable, 1):
        msg = generate(lead)
        lead["subject"] = msg["subject"]
        lead["body"] = msg["body"]
        print(f"  [{i}/{len(contactable)}] {lead['name']}")

    _save(analyzed, "data/leads.csv")
    print(f"[+] Done: {len(analyzed)} leads → data/leads.csv ({len(contactable)} with messages)")


if __name__ == "__main__":
    main()
