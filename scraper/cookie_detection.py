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

# Third-party resources that set non-essential cookies. Their presence is what
# creates the consent obligation — a site with none of these needs no banner.
_TRACKERS = (
    # Analytics
    "gtag(", "googletagmanager.com", "google-analytics.com", "_gaq",
    "clarity.ms", "hotjar.com", "hj(", "matomo.js", "piwik.js",
    # Advertising and social pixels
    "connect.facebook.net", "fbq(", "doubleclick.net", "googlesyndication.com",
    "googleadservices.com", "snap.licdn.com", "analytics.tiktok.com", "ads-twitter.com",
    # Embeds that set cookies on load
    "youtube.com/embed", "player.vimeo.com", "google.com/maps/embed",
    "addthis.com", "sharethis.com", "disqus.com",
)

# Deliberately NOT trackers:
# - plausible.io / usefathom.com — cookieless by design, no consent required
# - youtube-nocookie.com — the privacy-enhanced embed, which is the correct fix
# - fonts.googleapis.com — legally contested and present on nearly every site;
#   including it would flag almost everyone and destroy the check's credibility

# A hand-rolled banner is identified by a cookie word and a banner word sharing
# one id or class, so an unrelated ".cookie-recipe" is never mistaken for consent UI.
_GENERIC_BANNER_WORDS = ("cookie", "galleta")
_GENERIC_BANNER_CONTEXT = ("banner", "consent", "notice", "aviso", "bar", "popup", "modal", "gdpr", "rgpd", "lopd")


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


def _has_generic_banner(soup) -> bool:
    """Catch hand-rolled banners that use no known consent platform.

    Requires both a cookie word and a banner-ish word in the same id or class, so
    an unrelated ``.cookie-recipe`` on a bakery site is not mistaken for consent UI.
    """
    for el in soup.find_all(attrs={"id": True}):
        token = el["id"].lower()
        if any(w in token for w in _GENERIC_BANNER_WORDS) and any(c in token for c in _GENERIC_BANNER_CONTEXT):
            return True

    for el in soup.find_all(attrs={"class": True}):
        token = " ".join(el["class"]).lower()
        if any(w in token for w in _GENERIC_BANNER_WORDS) and any(c in token for c in _GENERIC_BANNER_CONTEXT):
            return True

    return False


def _loads_trackers(html_lower: str) -> bool:
    """True when the page loads a resource that sets non-essential cookies."""
    return any(sig in html_lower for sig in _TRACKERS)


def _has_cookie_policy(soup) -> bool:
    """True when the page links to an actual cookie policy page.

    Requires a navigable href: consent banners render their own "Aceptar cookies"
    and "Configurar cookies" controls as anchors, and counting those would report a
    policy page that does not exist.
    """
    for link in soup.find_all("a", href=True):
        href = link["href"].strip().lower()
        if href == "" or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if "cookie" in href or "cookie" in link.get_text(strip=True).lower():
            return True
    return False


_LEGAL_PAGE_PATTERNS: dict[str, tuple[str, ...]] = {
    "no_legal_notice": (
        "aviso legal", "avis legal", "nota legal",
        "/aviso-legal", "/avis-legal", "/legal",
    ),
    "no_privacy_policy": (
        "privacidad", "privacitat", "proteccion de datos", "protecció de dades",
        "/privacidad", "/privacy", "/proteccion-datos",
    ),
}


def detect_legal_pages(soup) -> list[str]:
    """Return issue keys for mandatory legal pages with no link on the page.

    Matches link text and href against Spanish and Valencian variants — a bilingual
    footer in Elche or Alicante would otherwise be reported as non-compliant while
    having every text published.
    """
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip().lower()
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        links.append(f"{a.get_text(strip=True).lower()} {href}")

    haystack = " | ".join(links)

    return [key for key, patterns in _LEGAL_PAGE_PATTERNS.items()
            if not any(p in haystack for p in patterns)]


# Input types that collect personal data. Submit, hidden, button and the rest are
# excluded so they never push a form over the field threshold below.
_DATA_INPUT_TYPES = (None, "text", "email", "tel")

# A form must collect at least this many fields to read as a contact form. A site
# search box is a single text input and would otherwise be reported as a form
# gathering personal data without consent.
_MIN_CONTACT_FIELDS = 2


def _form_lacks_consent(soup) -> bool:
    """True when a contact form collects data with no consent checkbox or privacy link."""
    for form in soup.find_all("form"):
        inputs = form.find_all(("input", "textarea"))
        field_count = sum(1 for i in inputs if i.get("type") in _DATA_INPUT_TYPES)
        if field_count < _MIN_CONTACT_FIELDS:
            continue

        has_checkbox = any(i.get("type") == "checkbox" for i in inputs)
        has_privacy_link = any(
            "privac" in a["href"].lower() or "privac" in a.get_text(strip=True).lower()
            for a in form.find_all("a", href=True)
        )

        if not has_checkbox and not has_privacy_link:
            return True

    return False


def detect_form_compliance(soup) -> list[str]:
    """Return issue keys for contact forms that gather personal data without consent."""
    return ["form_without_consent"] if _form_lacks_consent(soup) else []


def detect_cookie_compliance(html: str, soup) -> list[str]:
    """Return cookie-related compliance issues visible in static HTML.

    Reports a missing banner only when the site actually loads non-essential
    cookies. A site with no trackers has no consent obligation, so flagging it
    would tell a compliant business it is breaking the law — the most expensive
    mistake this analyser can make, because it is told to them on a sales call.

    Returns:
        A list containing zero or more of ``no_cookie_banner`` and
        ``no_cookie_policy``.
    """
    html_lower = html.lower()
    issues = []

    has_consent_ui = _has_cmp(html_lower, soup) or _has_generic_banner(soup)

    if _loads_trackers(html_lower) and not has_consent_ui:
        issues.append("no_cookie_banner")

    if not _has_cookie_policy(soup):
        issues.append("no_cookie_policy")

    return issues
