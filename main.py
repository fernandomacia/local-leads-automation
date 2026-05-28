import os
import pandas as pd

from config import HEADLESS, MAX_RESULTS, SEARCH_QUERY
from scraper.maps_scraper import scrape


def main():
    leads = scrape(SEARCH_QUERY, MAX_RESULTS, HEADLESS)

    os.makedirs("data", exist_ok=True)
    pd.DataFrame(leads).to_csv("data/leads.csv", index=False)
    print(f"[+] Saved {len(leads)} leads to data/leads.csv")


if __name__ == "__main__":
    main()
