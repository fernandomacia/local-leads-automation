import os
import pandas as pd

from config import DEFAULT_PROFESSION, DEFAULT_CITY, HEADLESS
from scraper.maps_scraper import scrape


def main():
    leads = scrape(DEFAULT_PROFESSION, DEFAULT_CITY, HEADLESS)

    os.makedirs("data", exist_ok=True)
    pd.DataFrame(leads).to_csv("data/leads.csv", index=False)
    print(f"[+] Saved {len(leads)} leads for {DEFAULT_CITY}")


if __name__ == "__main__":
    main()
