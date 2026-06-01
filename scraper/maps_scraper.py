import random
import time
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright, Page

from config import (
    DELAY_INITIAL_LOAD,
    DELAY_AFTER_CONSENT,
    DELAY_PER_CARD_CLICK,
    DELAY_SCROLL_AFTER_EXTRACT,
    JITTER_RANGE,
    RETRY_BACKOFF_BASE,
    MAX_EXTRACTION_RETRIES,
)


def _get_delay(base_delay: float) -> float:
    """Apply jitter to delay: base ± (base * JITTER_RANGE)."""
    variance = base_delay * JITTER_RANGE
    return base_delay + random.uniform(-variance, variance)


def _exponential_backoff(attempt: int) -> float:
    """Return delay for exponential backoff: 2, 4, 8, 16..."""
    return RETRY_BACKOFF_BASE ** attempt


def _handle_consent(page: Page) -> None:
    """Accept Google's EU cookie consent page if redirected."""
    if "consent.google.com" not in page.url:
        return
    page.locator('button:has-text("Aceptar todo")').first.click()
    page.wait_for_url("**/maps**", timeout=15000)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(_get_delay(DELAY_AFTER_CONSENT))


def _collect_hrefs(page: Page) -> list[str]:
    """Phase 1: Scroll the results list and collect all business URLs without clicking.

    Scrolling div[role=main] only works reliably while the list is active (no card open).
    Returns a deduplicated ordered list of place URLs.
    """
    seen: set[str] = set()
    no_new_count = 0

    while True:
        current_hrefs = [
            href for el in page.locator("a.hfpxzc").all()
            if (href := el.get_attribute("href"))
        ]
        new = [h for h in current_hrefs if h not in seen]
        seen.update(new)

        page.evaluate("""
            const feed = document.querySelector('div[role="feed"]');
            if (feed) feed.scrollTop += 600;
        """)
        time.sleep(_get_delay(DELAY_SCROLL_AFTER_EXTRACT))

        if new:
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= 3:
                break

    return list(seen)


def _extract_business(page: Page) -> dict:
    """Extract name and website from an open business page."""
    name = page.locator("h1.DUwDvf").inner_text()
    authority = page.locator('a[data-item-id="authority"]')
    website = authority.first.get_attribute("href") if authority.count() > 0 else ""
    return {"name": name, "website": website or ""}


def scrape(profession: str, city: str, headless: bool = False) -> list[dict]:
    """Scrape all business listings from Google Maps for a profession in a city.

    Args:
        profession: Profession to search (e.g., "abogados").
        city: City to search in (e.g., "Elche").
        headless: Run browser without UI.

    Returns:
        List of dicts with keys: name, website.
    """
    search_url = f"https://www.google.com/maps/search/{quote_plus(f'{profession} {city}')}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        page.goto(search_url)
        time.sleep(_get_delay(DELAY_INITIAL_LOAD))
        _handle_consent(page)
        page.wait_for_selector("a.hfpxzc", timeout=15000)

        print(f"[+] Collecting results for {profession} in {city}...")
        hrefs = _collect_hrefs(page)
        print(f"[+] Found {len(hrefs)} listings, extracting...")

        leads = []
        for i, href in enumerate(hrefs):
            retries = 0
            while retries < MAX_EXTRACTION_RETRIES:
                try:
                    page.goto(href)
                    page.wait_for_selector("h1.DUwDvf", timeout=10000)
                    time.sleep(_get_delay(DELAY_PER_CARD_CLICK))
                    lead = _extract_business(page)
                    print(f"  [{i + 1}/{len(hrefs)}] {lead['name']}")
                    leads.append(lead)
                    break
                except TimeoutError:
                    retries += 1
                    if retries < MAX_EXTRACTION_RETRIES:
                        wait_time = _exponential_backoff(retries)
                        print(f"    [retry {retries}] Timeout, waiting {wait_time:.1f}s...")
                        time.sleep(wait_time)
                except Exception as e:
                    print(f"  [!] Skipped listing {i + 1}: {type(e).__name__}")
                    break

        print(f"[+] Done. Total leads: {len(leads)}")
        browser.close()
        return leads
