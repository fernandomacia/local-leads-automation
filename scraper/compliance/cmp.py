"""Consent management platform (CMP) detection in static HTML.

Cookie banners are almost always injected by JavaScript, so the visible banner
is absent from the HTML we fetch. What *is* present is the script that builds
it — a vendor CDN URL, a plugin path on the site's own domain, or the
container the script fills in. Detection therefore targets the loader, never
the rendered banner.

Deliberately conservative: a false positive here is told to a business owner as
"you are breaking the law", so ambiguity resolves to "compliant".
"""

from .matching import contains_term, normalize
from .vocabulary import ACCEPT_TERMS, BANNER_CONTEXT, BANNER_WORDS, REJECT_TERMS

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

# Inline configuration blobs. Complianz, CookieYes and friends serialize their
# button labels into the page, which is where the reject control is visible even
# when the banner itself is built later.
_CMP_SCRIPT_MARKERS = (
    "cmplz", "cookieyes", "cky_", "cookie_law_info", "cookieconsent",
    "iubenda", "moove_frontend", "borlabs", "didomi", "usercentrics",
)


def _attribute_soup(el) -> str:
    """Every attribute value of an element and its descendants, space-joined.

    Consent controls carry their intent in ``data-cookie-action="reject"`` or in
    a BEM modifier as often as in their visible label, and the label itself is
    frequently the only part rendered server-side.
    """
    values = []
    for node in [el, *el.find_all(True)]:
        for value in node.attrs.values():
            values.append(" ".join(value) if isinstance(value, list) else str(value))
    return " ".join(values)


def has_cmp(html_lower: str, soup) -> bool:
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


def _is_banner_token(token: str) -> bool:
    """True when one id or class names both a cookie word and a banner word."""
    return any(w in token for w in BANNER_WORDS) and any(c in token for c in BANNER_CONTEXT)


def _element_tokens(soup):
    """Yield each element paired with its lowercased id and class attributes."""
    for el in soup.find_all(attrs={"id": True}):
        yield el, el["id"].lower()
    for el in soup.find_all(attrs={"class": True}):
        yield el, " ".join(el["class"]).lower()


def has_generic_banner(soup) -> bool:
    """Catch hand-rolled banners that use no known consent platform.

    Requires both a cookie word and a banner-ish word in the same id or class, so
    an unrelated ``.cookie-recipe`` on a bakery site is not mistaken for consent UI.
    """
    return any(_is_banner_token(token) for _, token in _element_tokens(soup))


def _banner_nodes(soup) -> list:
    """Return the elements that look like the consent banner's own container."""
    return [el for el, token in _element_tokens(soup)
            if _is_banner_token(token) or any(m in token for m in _CMP_DOM_MARKERS)]


def _consent_markup(soup) -> str:
    """Pooled, normalized text of everything that describes the banner's controls.

    Two sources: the banner container rendered server-side, and the inline CMP
    configuration, which carries the button labels even when the banner is not
    in the HTML.
    """
    parts = [f"{el.get_text(' ', strip=True)} {_attribute_soup(el)}" for el in _banner_nodes(soup)]
    for script in soup.find_all("script"):
        text = script.string or ""
        if text and any(m in text.lower() for m in _CMP_SCRIPT_MARKERS):
            parts.append(text)
    return normalize(" ".join(parts))


def reject_status(soup) -> str:
    """Report whether the banner offers a reject control in its first layer.

    Returns ``"ok"``, ``"missing"``, or ``"unknown"``. ``"missing"`` is only
    reported when an accept control *was* visible in the same markup: a banner
    whose controls are built entirely in JavaScript tells us nothing, and
    guessing there would accuse a compliant site of the single most heavily
    sanctioned cookie infringement.
    """
    markup = _consent_markup(soup)
    if not markup or not contains_term(markup, ACCEPT_TERMS):
        return "unknown"
    return "ok" if contains_term(markup, REJECT_TERMS) else "missing"
