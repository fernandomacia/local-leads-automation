import time
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright, Page


def _handle_consent(page: Page) -> None:
    """Accept Google's EU cookie consent page if redirected."""
    if "consent.google.com" not in page.url:
        return
    page.locator('button:has-text("Aceptar todo")').first.click()
    page.wait_for_url("**/maps**", timeout=15000)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(3)


def _extract_business(page: Page) -> dict:
    """Extract name and website from the currently open business panel."""
    name = page.locator("h1.DUwDvf").inner_text()
    authority = page.locator('a[data-item-id="authority"]')
    website = authority.first.get_attribute("href") if authority.count() > 0 else ""
    return {"name": name, "website": website or ""}


def scrape(query: str, max_results: int = 10, headless: bool = False) -> list[dict]:
    """Scrape business listings from Google Maps.

    Args:
        query: Search term (e.g., "abogados alicante").
        max_results: Maximum number of results to extract.
        headless: Run browser without UI.

    Returns:
        List of dicts with keys: name, website.
    """
    search_url = f"https://www.google.com/maps/search/{quote_plus(query)}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        page.goto(search_url)
        time.sleep(3)
        _handle_consent(page)

        page.wait_for_selector("a.hfpxzc", timeout=15000)
        cards = page.locator("a.hfpxzc")
        count = min(cards.count(), max_results)
        print(f"[+] Found {cards.count()} results, extracting {count}")

        leads = []

        for i in range(count):
            try:
                # Re-locate cards each iteration — the DOM re-renders after each panel opens
                page.locator("a.hfpxzc").nth(i).click()
                time.sleep(4)
                lead = _extract_business(page)
                print(f"  [{i + 1}/{count}] {lead['name']} | {lead['website'] or '—'}")
                leads.append(lead)
            except Exception as e:
                print(f"  [!] Error at result {i + 1}: {e}")

        browser.close()
        return leads
