import random
import re
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


def _collect_hrefs(page: Page, max_results: int | None = None) -> list[str]:
    """Scroll the results list and collect business URLs without clicking.

    Uses div[role="feed"] as the scroll target — only reliable while no card panel is open.
    Returns a deduplicated list of place URLs, capped at max_results if provided.
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

        if max_results and len(seen) >= max_results:
            break

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

    hrefs = list(seen)
    return hrefs[:max_results] if max_results else hrefs


def _extract_business(page: Page, default_city: str = "") -> dict:
    """Extract business info from an open Google Maps place page."""
    name = page.locator("h1.DUwDvf").inner_text()

    authority = page.locator('a[data-item-id="authority"]')
    website = authority.first.get_attribute("href") if authority.count() > 0 else ""

    phone_el = page.locator('a[href^="tel:"]')
    phone = phone_el.first.get_attribute("href").replace("tel:", "") if phone_el.count() > 0 else ""

    address_el = page.locator('button[data-item-id="address"]')
    raw = address_el.first.inner_text() if address_el.count() > 0 else ""
    # Strip leading icon characters (Google Maps private-use Unicode) and whitespace
    full_address = re.sub(r'^[^\w]+', '', raw).strip()

    # Parse structured location fields from "Street, CP City, Province" format
    location_match = re.search(r'\b\d{5}\b\s+([^,]+),\s*([^,]+)', full_address)
    if location_match:
        zip_code = re.search(r'\b\d{5}\b', full_address).group()
        city = location_match.group(1).strip()
        province = location_match.group(2).strip()
    else:
        zip_code = ""
        city = f"**{default_city}**" if default_city else ""
        province = ""

    # Keep only the street part (everything before the zip code)
    street_match = re.match(r'^(.+?),?\s*\b\d{5}\b', full_address)
    address = street_match.group(1).strip().rstrip(",").strip() if street_match else full_address

    return {
        "name": name,
        "website": website or "",
        "phone": phone,
        "address": address,
        "zip_code": zip_code,
        "city": city,
        "province": province,
    }


def scrape(profession: str, city: str, headless: bool = False, max_results: int | None = None) -> list[dict]:
    """Scrape business listings from Google Maps for a profession in a city.

    Args:
        profession: Profession to search (e.g., "abogados").
        city: City to search in (e.g., "Elche").
        headless: Run browser without UI.
        max_results: Cap on listings to collect. None means collect all.

    Returns:
        List of dicts with keys: name, website, phone, address, zip_code, city, province.
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
        hrefs = _collect_hrefs(page, max_results)
        print(f"[+] Found {len(hrefs)} listings, extracting...")

        leads = []
        for i, href in enumerate(hrefs):
            retries = 0
            while retries < MAX_EXTRACTION_RETRIES:
                try:
                    page.goto(href)
                    page.wait_for_selector("h1.DUwDvf", timeout=10000)
                    time.sleep(_get_delay(DELAY_PER_CARD_CLICK))
                    lead = _extract_business(page, city)
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
