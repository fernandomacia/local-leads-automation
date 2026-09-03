"""Detection of cookie consent management platforms (CMPs) in static HTML.

Cookie banners are almost always injected by JavaScript, so the visible banner is
absent from the HTML we fetch. What *is* present is the script that builds it —
either a vendor CDN URL or a plugin path on the site's own domain. Detection
therefore targets the loader, never the rendered banner.

Deliberately conservative: a false positive here is told to a business owner as
"you are breaking the law", so ambiguity resolves to "compliant".
"""

# Vendor CDNs and script hosts. Matched against the full HTML, case-insensitively.
_CMP_VENDORS = (
    "cookiebot.com", "consent.cookiebot",       # Cookiebot
    "cookieyes.com", "cookie-law-info",          # CookieYes / GDPR Cookie Consent
    "iubenda.com",                               # Iubenda
    "onetrust.com", "cookielaw.org",             # OneTrust
    "cookiehub.net",
    "termly.io",
    "osano.com",
    "usercentrics.eu",
    "didomi.io",
    "axeptio.eu",
    "tarteaucitron",
    "orestbida.com/implementation/cookieconsent",  # vanilla-cookieconsent
    "civicuk.com",                               # Civic Cookie Control
    "borlabs.io",
    "klaro",
    "quantcast.mgr.consensu.org",
)

# WordPress plugin paths. The Spanish market skews heavily to these.
_CMP_PLUGIN_PATHS = (
    "/plugins/complianz-gdpr/",
    "/plugins/cookie-law-info/",
    "/plugins/gdpr-cookie-compliance/",          # Moove
    "/plugins/cookie-notice/",
    "/plugins/borlabs-cookie/",
    "/plugins/wp-gdpr-compliance/",
    "/plugins/cookiebot/",
    "/plugins/asesor-cookies-para-la-ley-en-espana/",  # very common on ES small business sites
    "/plugins/italy-cookie-choices/",
)

# Container ids and classes the CMPs render. Catches self-hosted or renamed loaders.
_CMP_DOM_MARKERS = (
    "cmplz-",                    # Complianz
    "cky-",                      # CookieYes
    "cookie-notice",             # Cookie Notice
    "moove_gdpr",                # Moove
    "cybotcookiebotdialog",      # Cookiebot
    "onetrust-consent-sdk",
    "didomi-host",
    "axeptio_overlay",
    "iubenda-cs-banner",
    "termly-",
    "cc-window",                 # cookieconsent v3
    "borlabs-cookie",
    "segurseo-cookie-banner",    # our own theme — see the WordPress section
)


def _has_cmp(html_lower: str, soup) -> bool:
    """True when any consent-platform loader or container is present."""
    if any(sig in html_lower for sig in _CMP_VENDORS):
        return True
    if any(path in html_lower for path in _CMP_PLUGIN_PATHS):
        return True

    for el in soup.find_all(attrs={"id": True}):
        if any(m in el["id"].lower() for m in _CMP_DOM_MARKERS):
            return True

    for el in soup.find_all(attrs={"class": True}):
        classes = " ".join(el["class"]).lower()
        if any(m in classes for m in _CMP_DOM_MARKERS):
            return True

    # Cookiebot and Complianz block third-party scripts by rewriting them to
    # type="text/plain" with a consent attribute — a strong signal on its own.
    for script in soup.find_all("script", attrs={"type": "text/plain"}):
        if any(a.startswith("data-cookie") or a.startswith("data-cmplz") for a in script.attrs):
            return True

    return False


def _has_cookie_policy(soup) -> bool:
    """True when the page links to something that reads like a cookie policy."""
    for link in soup.find_all("a", href=True):
        text = link.get_text(strip=True).lower()
        href = link["href"].lower()
        if "cookie" in text or "cookie" in href:
            return True
    return False


def detect_cookie_compliance(html: str, soup) -> list[str]:
    """Return compliance issue keys for the cookie obligations we can see statically.

    Returns:
        A list containing zero or more of ``no_cookie_banner`` and
        ``no_cookie_policy``.
    """
    html_lower = html.lower()
    issues = []

    if not _has_cmp(html_lower, soup):
        issues.append("no_cookie_banner")
    if not _has_cookie_policy(soup):
        issues.append("no_cookie_policy")

    return issues
