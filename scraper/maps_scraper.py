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
from scraper.domains import normalize_domain
from scraper.social import identify_platform

# Google Maps DOM hooks — centralized so a class-name change only needs one edit
SELECTOR_RESULTS = "a.hfpxzc"
SELECTOR_NAME = "h1.DUwDvf"
SELECTOR_WEBSITE = 'a[data-item-id="authority"]'
SELECTOR_PHONE = 'a[href^="tel:"]'
SELECTOR_ADDRESS = 'button[data-item-id="address"]'
SELECTOR_FEED = 'div[role="feed"]'

SCROLL_PIXELS = 600

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
        pass  # No listings found — callers handle an empty page naturally

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


def _extract_business(page: Page, default_city: str = "") -> dict:
    """Extract structured business data from an open Google Maps place page.

    Address parsing targets the Spanish postal format: "Street, CP City, Province". When
    the zip code is absent, the city falls back to the one that was searched for.

    That fallback used to be wrapped in asterisks — ``**Elche**`` — to mark it as inferred
    rather than parsed. Nothing ever read the marker: not this worker, not the API, not the
    panel. What it did do was travel into ``leads.city`` and stay there, so a lead sorted
    under ``*`` on the panel's city column and an agent opening the lead read the asterisks.
    The inference is legible without it anyway, and more precisely: an unparsed address is
    exactly the row whose ``zip_code`` and ``province`` came back empty.
    """
    name = page.locator(SELECTOR_NAME).inner_text()

    authority = page.locator(SELECTOR_WEBSITE)
    href = authority.first.get_attribute("href") if authority.count() > 0 else ""
    # A card whose website button is a social profile has no website here, and the
    # profile is not one either: it is recorded as the network it is, and the mismatch
    # becomes a finding of its own.
    platform = identify_platform(href) if href else None
    website = "" if platform else canonical_website(href)
    social_networks = {platform: _social_profile(href)} if platform else {}

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
        city = default_city
        province = ""

    street_match = re.match(r'^(.+?),?\s*\b\d{5}\b', full_address)
    address = street_match.group(1).strip().rstrip(",").strip() if street_match else full_address

    return {
        "lead": name,
        "website": website or "",
        "social_networks": social_networks,
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
                time.sleep(_exponential_backoff(retries))
        except PlaywrightError:
            return None
    return None


# Hosts that give each customer a path instead of a subdomain, with the number of
# segments that identify the customer's own site. Trimming those to the host would
# audit the builder's marketing homepage and file the findings under the lead.
_PATH_HOSTED_SITES: dict[str, int] = {
    "wixsite.com": 1,          # user.wixsite.com/minegocio
    "sites.google.com": 2,     # sites.google.com/view/minegocio
    "linktr.ee": 1,
    "taplink.cc": 1,
    "bio.link": 1,
    "beacons.ai": 1,
    "about.me": 1,
}


def _path_segments_to_keep(host: str) -> int:
    """Return how many path segments identify the site on this host, 0 for most."""
    host = host.lower().removeprefix("www.")
    return next((n for h, n in _PATH_HOSTED_SITES.items()
                 if host == h or host.endswith(f".{h}")), 0)


def canonical_website(href: str) -> str:
    """Return the business's site as it should be recorded, from the card's link.

    What the card carries is whatever the owner typed into Google Business Profile,
    and for anyone with an agency that is routinely a tracked landing page rather
    than the site: a real lead arrived as
    ``/en/hondon-de-las-nieves-lawyers?utm_source=Google&utm_medium=My%20Business``.
    Stored verbatim, that is the page the analyzer audits — so a law firm in Alicante
    was scored on an English satellite landing page and reported as an English-language
    site, which is what the agent was then told to call in.

    Three rules, and the third is the one with an exception behind it:

    - The query and the fragment always go. Tracking is never part of the site.
    - On a host that sells paths rather than subdomains, the segments that name the
      customer's site stay — ``wixsite.com`` trimmed to its host is Wix's own homepage.
    - Everything else reduces to scheme and host.

    Userinfo is dropped with the rest: credentials in a Maps listing are a mistake, and
    they would travel to the panel and into every request the analysis makes.
    """
    href = (href or "").strip()
    try:
        parsed = urlparse(href)
    except ValueError:
        return href

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return href

    host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    root = f"{parsed.scheme}://{host}"

    if keep := _path_segments_to_keep(parsed.hostname):
        segments = [seg for seg in parsed.path.split("/") if seg][:keep]
        return f"{root}/{'/'.join(segments)}" if segments else root

    return root


def _social_profile(href: str) -> str:
    """Return the profile URL as it should be recorded, tracking removed.

    The path stays, unlike :func:`canonical_website`: on these cards the path *is* the
    business — ``linkedin.com`` without it is the network's own homepage — and for a
    business with no site of its own it is the only address anyone can be sent to.
    """
    parsed = urlparse(href.strip())
    host = parsed.hostname + (f":{parsed.port}" if parsed.port else "") if parsed.hostname else ""
    return f"{parsed.scheme}://{host}{parsed.path}".rstrip("/")


def scrape_incrementally(
    profession: str,
    city: str,
    headless: bool = False,
    max_results: int | None = None,
    is_known=None,
):
    """Yield business listings one at a time as they're discovered on Google Maps.

    Never collects the full results list upfront: it scrolls in waves, extracting each
    newly visible card as it appears, which lets the worker report leads in batches as they
    are found instead of waiting for the whole search to finish. Each card is extracted in
    a disposable tab so the results feed itself never navigates away and loses its scroll
    position.

    Args:
        profession: Search term for the type of business.
        city: City to search in.
        headless: Run the browser without a visible window.
        max_results: Stop once this many non-skipped leads have been yielded.
            ``None`` collects until the search is exhausted.
        is_known: Asked about each extracted lead's normalized domain (see
            ``normalize_domain``) and, when it answers True, the lead is
            skipped silently and does not count toward ``max_results``.
            Asked per business rather than handed a set up front, because a
            set can only hold what the caller already knew: with
            ``max_results`` small — the point of a quick sample — a batched
            answer arrived after the cap had already been spent on businesses
            the system had all along. The card is still opened either way; it
            is the only place the website comes from.

    Yields:
        Lead dicts, one at a time, with keys: ``lead``, ``website``, ``phone``,
        ``address``, ``zip_code``, ``city``, ``province``, ``maps_url``.
    """
    is_known = is_known or (lambda domain: False)

    with sync_playwright() as p:
        browser, context, list_page = _start_search(p, profession, city, headless)
        try:
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
                    name = lead.get("lead", "")
                    domain = normalize_domain(lead.get("website", ""))
                    if lead["website"] and is_known(domain):
                        continue
                    if not lead["website"]:
                        if name in seen_names:
                            continue
                        seen_names.add(name)

                    found_new = True
                    yielded += 1
                    yield lead
                    if max_results and yielded >= max_results:
                        return

                idle_scrolls = 0 if new_hrefs else idle_scrolls + 1
                if idle_scrolls >= MAX_IDLE_SCROLLS:
                    break

                _scroll_feed(list_page)
        finally:
            browser.close()
