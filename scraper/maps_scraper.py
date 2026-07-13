"""Google Maps scraper for local business discovery.

Drives a real Chromium browser via Playwright to avoid bot detection.
The flow is: search → scroll to collect all listing URLs → visit each card
to extract structured business data.
"""

import random
import re
import time
from urllib.parse import quote_plus, urlparse
from playwright.sync_api import (
    sync_playwright,
    Browser,
    BrowserContext,
    Page,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from config import (
    DELAY_INITIAL_LOAD,
    DELAY_AFTER_CONSENT,
    DELAY_PER_CARD_CLICK,
    DELAY_SCROLL_AFTER_EXTRACT,
    JITTER_RANGE,
    RETRY_BACKOFF_BASE,
    MAX_EXTRACTION_RETRIES,
    MAX_IDLE_SCROLLS,
)

# Google Maps DOM hooks — centralized so a class-name change only needs one edit
SELECTOR_RESULTS = "a.hfpxzc"
SELECTOR_NAME = "h1.DUwDvf"
SELECTOR_WEBSITE = 'a[data-item-id="authority"]'
SELECTOR_PHONE = 'a[href^="tel:"]'
SELECTOR_ADDRESS = 'button[data-item-id="address"]'
SELECTOR_FEED = 'div[role="feed"]'

SCROLL_PIXELS = 600

# Maps card fields the AI can use as pitch arguments — parallel to SEO_ISSUE_LABELS in web_analyzer.py
MAPS_ISSUE_LABELS: dict[str, str] = {
    "no_website": "Sin sitio web en Google Maps",
    "no_phone":   "Sin teléfono en Google Maps",
    "no_address": "Sin dirección en Google Maps",
}


def _get_delay(base_delay: float) -> float:
    """Return base_delay with ±JITTER_RANGE% random variance to mimic human timing."""
    variance = base_delay * JITTER_RANGE
    return base_delay + random.uniform(-variance, variance)


def _exponential_backoff(attempt: int) -> float:
    return RETRY_BACKOFF_BASE ** attempt


def _handle_consent(page: Page) -> None:
    """Accept Google's EU cookie consent banner if present before Maps loads."""
    if "consent.google.com" not in page.url:
        return
    page.locator('button:has-text("Aceptar todo")').first.click()
    page.wait_for_url("**/maps**", timeout=15000)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(_get_delay(DELAY_AFTER_CONSENT))


def _start_search(p, profession: str, city: str, headless: bool) -> tuple[Browser, BrowserContext, Page]:
    """Launch a browser, load the Maps results page, and accept consent.

    Returns the context alongside the browser/page so callers that need extra
    tabs (e.g. ``scrape_incrementally``) can open them from the same context —
    a fresh ``browser.new_page()`` would start its own isolated session and
    hit the consent screen again.
    """
    search_url = f"https://www.google.com/maps/search/{quote_plus(f'{profession} {city}')}"

    browser = p.chromium.launch(headless=headless)
    context = browser.new_context()
    page = context.new_page()

    page.goto(search_url)
    time.sleep(_get_delay(DELAY_INITIAL_LOAD))
    _handle_consent(page)
    try:
        page.wait_for_selector(SELECTOR_RESULTS, timeout=15000)
    except PlaywrightTimeoutError:
        # No listings found (e.g. small town with no matches) — callers handle an empty page naturally
        print(f"[!] No results found for '{profession}' in '{city}'")

    return browser, context, page


def _visible_listing_hrefs(page: Page) -> list[str]:
    """Return the href of every listing card currently rendered in the results feed."""
    return [
        href for el in page.locator(SELECTOR_RESULTS).all()
        if (href := el.get_attribute("href"))
    ]


def _scroll_feed(page: Page) -> None:
    page.evaluate(f"""
        const feed = document.querySelector('{SELECTOR_FEED}');
        if (feed) feed.scrollTop += {SCROLL_PIXELS};
    """)
    time.sleep(_get_delay(DELAY_SCROLL_AFTER_EXTRACT))


def _collect_hrefs(page: Page, max_results: int | None = None) -> list[str]:
    """Scroll the results feed and collect all business URLs without opening cards.

    Scrolls the results feed — only reliable while no card panel is open.
    Stops when ``max_results`` is reached or ``MAX_IDLE_SCROLLS`` consecutive
    scrolls yield nothing new. Returns a deduplicated list capped at ``max_results``.
    """
    seen: set[str] = set()
    no_new_count = 0

    while True:
        new = [h for h in _visible_listing_hrefs(page) if h not in seen]
        seen.update(new)

        if max_results and len(seen) >= max_results:
            break

        _scroll_feed(page)

        if new:
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= MAX_IDLE_SCROLLS:
                break

    hrefs = list(seen)
    return hrefs[:max_results] if max_results else hrefs


def _extract_business(page: Page, default_city: str = "") -> dict:
    """Extract structured business data from an open Google Maps place page.

    Address parsing targets the Spanish postal format: "Street, CP City, Province".
    When the zip code is absent and a default_city is provided, city is stored as
    ``**city**`` to signal that the value was inferred rather than parsed.
    """
    name = page.locator(SELECTOR_NAME).inner_text()

    authority = page.locator(SELECTOR_WEBSITE)
    website = authority.first.get_attribute("href") if authority.count() > 0 else ""

    phone_el = page.locator(SELECTOR_PHONE)
    phone = phone_el.first.get_attribute("href").replace("tel:", "") if phone_el.count() > 0 else ""

    address_el = page.locator(SELECTOR_ADDRESS)
    raw = address_el.first.inner_text() if address_el.count() > 0 else ""
    # Google Maps prepends private-use Unicode characters as icons
    full_address = re.sub(r'^[^\w]+', '', raw).strip()

    location_match = re.search(r'\b\d{5}\b\s+([^,]+),\s*([^,]+)', full_address)
    if location_match:
        zip_code = re.search(r'\b\d{5}\b', full_address).group()
        city = location_match.group(1).strip()
        province = location_match.group(2).strip()
    else:
        zip_code = ""
        city = f"**{default_city}**" if default_city else ""
        province = ""

    street_match = re.match(r'^(.+?),?\s*\b\d{5}\b', full_address)
    address = street_match.group(1).strip().rstrip(",").strip() if street_match else full_address

    return {
        "lead": name,
        "website": website or "",
        "phone": phone,
        "address": address,
        "zip_code": zip_code,
        "city": city,
        "province": province,
    }


def _extract_with_retries(page: Page, href: str, default_city: str = "") -> dict | None:
    """Visit a listing and extract its data, retrying on timeout.

    Returns:
        The extracted lead dict, or ``None`` if extraction ultimately failed.
    """
    retries = 0
    while retries < MAX_EXTRACTION_RETRIES:
        try:
            page.goto(href)
            page.wait_for_selector(SELECTOR_NAME, timeout=10000)
            time.sleep(_get_delay(DELAY_PER_CARD_CLICK))
            lead = _extract_business(page, default_city)
            lead["maps_url"] = href
            return lead
        except PlaywrightTimeoutError:
            retries += 1
            if retries < MAX_EXTRACTION_RETRIES:
                wait_time = _exponential_backoff(retries)
                print(f"    [retry {retries}] Timeout, waiting {wait_time:.1f}s...")
                time.sleep(wait_time)
        except PlaywrightError as e:
            print(f"  [!] Skipped listing: {type(e).__name__}")
            return None
    return None


def _normalize_domain(url: str) -> str:
    """Normalize a URL to a bare lowercase hostname (no protocol, port, or www.).

    Matches the normalization rule used by the API (``App\\Values\\Domain``) so
    extracted websites can be compared against its ``known_domains`` lists.
    """
    if not url:
        return ""
    return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")


def scrape(profession: str, city: str, headless: bool = False, max_results: int | None = None) -> list[dict]:
    """Scrape business listings from Google Maps for a given profession and city.

    Args:
        profession: Search term for the type of business (e.g., ``"abogados"``).
        city: City to search in (e.g., ``"Elche"``).
        headless: Run the browser without a visible window.
        max_results: Maximum number of listings to collect. ``None`` collects all.

    Returns:
        List of dicts with keys: ``lead``, ``website``, ``phone``, ``address``,
        ``zip_code``, ``city``, ``province``, ``maps_url``.
    """
    with sync_playwright() as p:
        browser, _, page = _start_search(p, profession, city, headless)

        print(f"[+] Collecting results for {profession} in {city}...")
        hrefs = _collect_hrefs(page, max_results)
        print(f"[+] Found {len(hrefs)} listings, extracting...")

        leads = []
        for i, href in enumerate(hrefs):
            lead = _extract_with_retries(page, href, city)
            if lead is None:
                continue
            print(f"  [{i + 1}/{len(hrefs)}] {lead['lead']}")
            leads.append(lead)

        print(f"[+] Done. Total leads: {len(leads)}")
        browser.close()
        return leads


def scrape_incrementally(
    profession: str,
    city: str,
    headless: bool = False,
    max_results: int | None = None,
    skip: set[str] | None = None,
):
    """Yield business listings one at a time as they're discovered on Google Maps.

    Unlike ``scrape()``, this never collects the full results list upfront —
    it scrolls in waves, extracting each newly visible card as it appears, which
    lets a worker process leads (and report them in batches) as they're found
    instead of waiting for the whole search to finish. Each card is extracted in
    a disposable tab so the results feed itself never navigates away and loses
    its scroll position.

    Args:
        profession: Search term for the type of business.
        city: City to search in.
        headless: Run the browser without a visible window.
        max_results: Stop once this many non-skipped leads have been yielded.
            ``None`` collects until the search is exhausted.
        skip: Normalized website domains (see ``_normalize_domain``) to silently
            skip — already known to the caller. Skipped leads don't count
            toward ``max_results``.

    Yields:
        Lead dicts with the same shape as ``scrape()``'s results, one at a time.
    """
    skip = skip or set()

    with sync_playwright() as p:
        browser, context, list_page = _start_search(p, profession, city, headless)
        try:
            print(f"[+] Collecting results for {profession} in {city}...")
            seen_hrefs: set[str] = set()
            seen_names: set[str] = set()  # dedup no-website leads within this session
            yielded = 0
            idle_scrolls = 0

            while True:
                new_hrefs = [h for h in _visible_listing_hrefs(list_page) if h not in seen_hrefs]
                seen_hrefs.update(new_hrefs)

                found_new = False
                for href in new_hrefs:
                    detail_page = context.new_page()
                    lead = _extract_with_retries(detail_page, href, city)
                    detail_page.close()
                    if lead is None:
                        continue
                    if lead["website"] and _normalize_domain(lead["website"]) in skip:
                        continue
                    if not lead["website"]:
                        name = lead.get("lead", "")
                        if name in seen_names:
                            continue
                        seen_names.add(name)

                    found_new = True
                    yielded += 1
                    yield lead
                    if max_results and yielded >= max_results:
                        return

                idle_scrolls = 0 if found_new else idle_scrolls + 1
                if idle_scrolls >= MAX_IDLE_SCROLLS:
                    print(f"[!] Stopping: {MAX_IDLE_SCROLLS} consecutive scrolls with no new lead")
                    break

                _scroll_feed(list_page)
        finally:
            browser.close()
