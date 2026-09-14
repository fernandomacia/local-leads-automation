"""Browser rendering fallback for sites whose footer is built by JavaScript.

The static fetch sees the HTML the server sends, which on a growing share of
sites contains no footer at all — it is assembled client-side, legal links
included. Those sites would be reported as having no legal texts whatsoever,
which is both wrong and obviously wrong to the owner reading it.

Used sparingly: only when the static pass finds no legal link of any kind, on a
site we already fetched successfully. Chromium is already installed for the
Maps scraper, so this costs seconds, not a dependency.
"""

import logging

from playwright.sync_api import sync_playwright

from config import RENDER_TIMEOUT_MS
from scraper.net_guard import host_ok

logger = logging.getLogger(__name__)

# Enough scrolling to trigger the lazy-loaded footers this fallback exists for.
_SCROLL_PIXELS = 30000
_SETTLE_MS = 800

# A navigation that never completed leaves an empty document behind. Returning it
# would be far worse than not rendering at all: an empty DOM has no legal links,
# no banner and no trackers, so every check would read as a finding.
_MIN_RENDERED_CHARS = 500


def render(url: str) -> str | None:
    """Return the page's DOM after JavaScript has run, or None if it cannot be rendered.

    Never raises: this is a best-effort second opinion, and a browser failure
    must not cost the lead its whole analysis.
    """
    # Checked here rather than trusted from the caller: a browser is the worst
    # client to hand an unvalidated URL, since it follows redirects and loads
    # subresources on its own, reaching hosts this code never sees.
    if not host_ok(url):
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=RENDER_TIMEOUT_MS)
                except Exception:
                    # One slow third-party script must not discard a page that is
                    # otherwise fully built; take whatever has loaded so far. The
                    # size check below is what separates that from a failed load.
                    pass
                # Chromium resolves redirects itself, so where it ended up is
                # checked before its DOM is trusted.
                if not host_ok(page.url):
                    return None
                try:
                    page.mouse.wheel(0, _SCROLL_PIXELS)
                    page.wait_for_timeout(_SETTLE_MS)
                    html = page.content()
                except Exception:
                    return None
                return html if len(html) >= _MIN_RENDERED_CHARS else None
            finally:
                browser.close()
    except Exception:
        logger.warning("Render fallback failed for %s", url, exc_info=True)
        return None
