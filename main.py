import os
import pandas as pd

from config import DEFAULT_PROFESSION, DEFAULT_CITY, HEADLESS
from scraper.maps_scraper import scrape
from scraper.web_analyzer import analyze

# zip_code must stay as string to preserve leading zeros (e.g. "03330")
_CSV_DTYPES = {"zip_code": str}


def _save(leads: list[dict], path: str) -> None:
    pd.DataFrame(leads).to_csv(path, index=False)


def _load(path: str) -> list[dict]:
    return pd.read_csv(path, dtype=_CSV_DTYPES).fillna("").to_dict("records")


def main():
    os.makedirs("data", exist_ok=True)

    # Phase 1 — scrape Google Maps
    leads = scrape(DEFAULT_PROFESSION, DEFAULT_CITY, HEADLESS)
    _save(leads, "data/leads_raw.csv")
    print(f"[+] Phase 1 complete: {len(leads)} leads → data/leads_raw.csv")

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
    print(f"[+] Phase 2 complete: {len(analyzed)} leads → data/leads.csv")


if __name__ == "__main__":
    main()
