"""Google Maps scraper for local business discovery.

Drives a real Chromium browser via Playwright to avoid bot detection.
The flow is: search → scroll to collect all listing URLs → visit each card
to extract structured business data.
"""

import random
import re
import time
from urllib.parse import quote_plus, urlparse

import idna
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

# Maps card fields the AI can use as pitch arguments — parallel to SEO_ISSUE_LABELS
# in web_analyzer.py. Issue key -> (job field that must be populated, pitch label).
# Both halves live in one entry so an issue cannot be declared with a field and no
# label, or with a label the detector never produces.
MAPS_ISSUES: dict[str, tuple[str, str]] = {
    "no_website": ("website", "Sin sitio web en Google Maps"),
    "no_phone":   ("phone",   "Sin teléfono en Google Maps"),
    "no_address": ("address", "Sin dirección en Google Maps"),
}


def maps_card_issues(lead: dict) -> dict[str, str]:
    """Return what this business's Google Maps card is missing.

    Read here, from the scraped card, and reported with the ingest batch — never
    re-derived later. The analysis step used to compute it from the job the API handed
    back, which loses the one finding it exists for: an agent supplies a website through
    the panel for a business whose card had none, the worker then sees a job *with* a
    website, and "no website on the listing" silently becomes "the listing was complete".
    The overwrite was invisible because it was a well-formed reading of the wrong thing.

    An empty dict is a real answer — the card is complete — and is what makes NULL on the
    API side mean "no worker has reported on this lead", so callers must send it as it
    comes rather than omitting it when empty.
    """
    return {key: label for key, (field, label) in MAPS_ISSUES.items() if not lead.get(field)}


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
                time.sleep(_exponential_backoff(retries))
        except PlaywrightError:
            return None
    return None


def _to_ascii(host: str) -> str:
    """Punycode a hostname, or return it as written if it cannot be encoded.

    Mirrors ``Domain::toAscii()``: falling back to the name as written rather than to an
    empty string, which would compare equal to everything. An already-ASCII host is
    returned untouched rather than round-tripped, because ``idna`` is stricter than the
    profile the API uses and would reject hosts PHP accepts.

    **The two are not identical in that fallback, and it is bounded on purpose.** PHP runs
    ICU's UTS-46, which encodes anything it can parse — an emoji label, a mixed
    underscore-and-accent host — while ``idna`` enforces IDNA2008 validity and refuses
    them. Where it refuses, this returns the name as written and the API's answer will not
    match it. The cost of a miss is one slot in ``max_results``, never a duplicate lead:
    the skip set is an optimisation, and the API normalises what it is *sent* at ingest, so
    deduplication there is unaffected. See ``TestNormalizeDomainAgreesWithTheApi``.
    """
    if not host or host.isascii():
        return host

    try:
        return idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError:
        return host


def _normalize_domain(url: str) -> str:
    """Normalize a URL to the bare ASCII hostname the API stores.

    A port-for-port translation of ``App\\Values\\Domain::from()``, and it has to stay
    one: ``POST /domains/check`` answers with domains *it* normalised, and this is what
    those answers are compared against. The two disagreed on six of the nine cases in
    ``TestNormalizeDomain``, and every disagreement is a business re-ingested, re-analysed
    at the owner's expense and rung again:

    - **No punycode.** The API stores ``xn--peluquera-n5a.es``, this returned
      ``peluquería.es``, so no accented domain ever matched — and Spanish domains with an
      ñ or an accent are not an edge case in this market.
    - **``netloc`` is not a hostname.** It carries the port *and* any userinfo, so
      ``https://user:pass@ejemplo.es/x`` normalised to ``user``. ``.hostname`` is the field
      that means what this function means.
    - **A URL with no scheme produced the empty string**, because ``urlparse`` needs one to
      see a host at all — so a bare ``ejemplo.es`` from a Maps card matched nothing.
    - **A trailing dot survived**, and ``ejemplo.es.`` is the same site as ``ejemplo.es``.
    """
    url = url.strip()

    if not url:
        return ""

    try:
        # The "//" prefix is what lets a scheme-less "www.ejemplo.es/tienda" parse, and is
        # the same fallback the API applies for the same reason.
        host = urlparse(url).hostname or urlparse(f"//{url}").hostname or url
    except ValueError:
        host = url

    return _to_ascii(host.rstrip(".").lower().removeprefix("www."))


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
        try:
            hrefs = _collect_hrefs(page, max_results)
            leads = []
            for href in hrefs:
                lead = _extract_with_retries(page, href, city)
                if lead is not None:
                    leads.append(lead)
            return leads
        finally:
            # Belt and braces, matching scrape_incrementally: sync_playwright()'s
            # __exit__ already stops the driver — and the browsers it spawned — on
            # any exception, Ctrl+C included, so this closes at the point of failure
            # rather than adding cleanup that was otherwise missing.
            browser.close()


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
            toward ``max_results``, but they are still extracted: the card has
            to be opened to learn the website the domain comes from.

    Yields:
        Lead dicts with the same shape as ``scrape()``'s results, one at a time.
    """
    skip = skip or set()

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
                    domain = _normalize_domain(lead.get("website", ""))
                    if lead["website"] and domain in skip:
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
