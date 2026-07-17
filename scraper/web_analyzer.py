"""Website analysis for CMS detection, contact extraction, and SEO scoring.

Fetches each lead's website with a standard HTTP client and inspects the HTML
for WordPress/platform signals, email addresses, social links, and common
on-page SEO issues. Social-only URLs (Instagram, Facebook, etc.) are detected
early and routed out without a full fetch.
"""

import re
import urllib3
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import SOCIAL_DOMAINS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 15
CONNECT_TIMEOUT = 12        # generous connect timeout — slow servers need it
PROBE_TIMEOUT = 5           # secondary HEAD probes (sitemap, robots.txt)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# Ordered by specificity — first match wins
CMS_SIGNATURES: list[tuple[str, list[str]]] = [
    ("wordpress",   ["wp-content/", "wp-json/", "wp-includes/"]),
    ("wix",         ["wix.com/", "wixsite.com", "X-Wix-Published-Version"]),
    ("squarespace", ["squarespace.com", "squarespace-cdn.com"]),
    ("shopify",     ["cdn.shopify.com", "Shopify.theme"]),
    ("prestashop",  ["prestashop", "PrestaShop"]),
    ("joomla",      ["/media/jui/", "Joomla!"]),
    ("drupal",      ["Drupal.settings", "/sites/default/files/"]),
    ("webflow",     ["data-wf-page", "webflow.com"]),
    ("typo3",       ["typo3conf/", "typo3/"]),
    ("jimdo",       ["jimdo.com", "jimdofree.com", "jimdosite.com"]),
    ("blogger",     ["blogger.com", "blogspot.com"]),
    ("magento",     ["Mage.Cookies", "/skin/frontend/", "magento"]),
    ("webnode",     ["webnode.com", "webnode.es"]),
    ("ghost",       ["ghost.io", "content/themes/"]),
]

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_CONTACT_PATHS = ("/contacto", "/contact", "/contactar")


def _fetch(url: str, *, log_failure: bool = False) -> tuple[str, BeautifulSoup] | None:
    """Fetch a URL and return ``(raw_html, soup)``. Returns ``None`` on any failure.

    Args:
        log_failure: Print the failure reason (HTTP status, DNS error, timeout, etc.).
            Used for the primary site fetch in ``analyze()``; contact-page probes in
            ``_extract_email`` stay silent since 404s there are expected and frequent.
    """
    try:
        resp = requests.get(url, timeout=(CONNECT_TIMEOUT, TIMEOUT), headers=HEADERS, allow_redirects=True, verify=False)
        resp.raise_for_status()
        return resp.text, BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        if log_failure:
            print(f"  [!] Unreachable: {url} — {e}")
        return None


def _identify_platform(url: str) -> str | None:
    """Return the social platform name if the URL belongs to one, else ``None``."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for platform, domain in SOCIAL_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return None


def _detect_cms(html: str) -> str:
    """Return the CMS name detected in the HTML, or ``'unknown'`` if none matches."""
    for cms, signatures in CMS_SIGNATURES:
        if any(sig in html for sig in signatures):
            return cms
    return "unknown"


def _extract_email(soup: BeautifulSoup, base_url: str) -> str:
    """Find an email address on the page, falling back to common contact sub-pages.

    Priority: mailto link → regex match in page text → same checks on /contacto,
    /contact, and /contactar.
    """
    mailto = soup.find("a", href=re.compile(r"^mailto:", re.I))
    if mailto:
        return mailto["href"][7:].split("?")[0].strip().lower()

    emails = _EMAIL_RE.findall(soup.get_text())
    if emails:
        return emails[0].lower()

    for path in _CONTACT_PATHS:
        result = _fetch(urljoin(base_url, path))
        if not result:
            continue
        _, contact_soup = result
        mailto = contact_soup.find("a", href=re.compile(r"^mailto:", re.I))
        if mailto:
            return mailto["href"][7:].split("?")[0].strip().lower()
        emails = _EMAIL_RE.findall(contact_soup.get_text())
        if emails:
            return emails[0].lower()

    return ""


def _extract_socials(soup: BeautifulSoup) -> dict[str, str]:
    """Return a dict mapping each social platform to the first matching link found."""
    found = {p: "" for p in SOCIAL_DOMAINS}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        for platform, domain in SOCIAL_DOMAINS.items():
            if not found[platform] and domain in href:
                found[platform] = href
    return found


def _url_exists(url: str) -> bool:
    try:
        resp = requests.head(url, timeout=(CONNECT_TIMEOUT, PROBE_TIMEOUT), headers=HEADERS, allow_redirects=True, verify=False)
        return resp.status_code < 400
    except requests.RequestException:
        return False


SEO_ISSUE_LABELS: dict[str, str] = {
    "no_https":           "Sin certificado SSL (web no segura)",
    "no_title":           "Sin título de página",
    "no_meta_description":"Sin descripción para buscadores (meta description)",
    "no_h1":              "Sin título principal (H1)",
    "multiple_h1":        "Múltiples títulos principales (H1)",
    "no_viewport":        "No adaptada a dispositivos móviles",
    "no_canonical":       "Sin URL canónica",
    "no_lang":            "Sin idioma declarado en el HTML",
    "no_og_tags":         "Sin etiquetas para redes sociales (Open Graph)",
    "no_structured_data": "Sin datos estructurados (Schema.org)",
    "no_alt_images":      "Imágenes sin texto alternativo (alt)",
    "no_analytics":       "Sin herramienta de analítica web",
    "no_favicon":         "Sin icono de la web (favicon)",
    "no_sitemap":         "Sin mapa del sitio (sitemap.xml)",
    "no_robots":          "Sin archivo robots.txt",
}


def _score_seo(soup: BeautifulSoup, url: str) -> tuple[int, list[str]]:
    """Audit the page for common SEO issues and return a score and issue list.

    Each issue deducts 10 points from 100. Checks cover security (HTTPS),
    on-page tags (title, meta, h1, viewport, canonical, lang, OG, structured data,
    alt attributes), analytics presence, favicon, and crawlability (sitemap,
    robots.txt). The last two require one extra HEAD request each.

    Returns:
        A tuple of ``(score, issues)`` where score is clamped to ``[0, 100]``
        and issues is a list of string keys describing each failing check.
    """
    issues = []
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    if not url.startswith("https://"):
        issues.append("no_https")

    if not soup.find("title"):
        issues.append("no_title")
    if not soup.find("meta", attrs={"name": "description"}):
        issues.append("no_meta_description")
    h1_tags = soup.find_all("h1")
    if not h1_tags:
        issues.append("no_h1")
    elif len(h1_tags) > 1:
        issues.append("multiple_h1")
    if not soup.find("meta", attrs={"name": "viewport"}):
        issues.append("no_viewport")
    if not soup.find("link", attrs={"rel": "canonical"}):
        issues.append("no_canonical")
    if not (soup.find("html") and soup.find("html").get("lang")):
        issues.append("no_lang")
    if not soup.find("meta", attrs={"property": "og:title"}):
        issues.append("no_og_tags")
    if not soup.find("script", attrs={"type": "application/ld+json"}):
        issues.append("no_structured_data")
    if any(img for img in soup.find_all("img") if img.get("alt") is None):
        issues.append("no_alt_images")

    # GA4, GTM, Universal Analytics, and legacy _gaq all count as "analytics present"
    scripts = " ".join((s.string or "") + " " + (s.get("src") or "") for s in soup.find_all("script"))
    if not any(s in scripts for s in ("gtag(", "googletagmanager.com", "google-analytics.com", "_gaq")):
        issues.append("no_analytics")

    if not soup.find("link", rel=lambda r: r and "icon" in (r if isinstance(r, list) else [r])):
        issues.append("no_favicon")

    # Each of these requires an extra HEAD request
    if not _url_exists(f"{base_url}/sitemap.xml"):
        issues.append("no_sitemap")
    if not _url_exists(f"{base_url}/robots.txt"):
        issues.append("no_robots")

    return max(0, 100 - len(issues) * 10), issues


# Default field values for leads whose website cannot be analyzed
_EMPTY_ANALYSIS: dict = {
    "cms": "", "email": "",
    **{p: "" for p in SOCIAL_DOMAINS},
    "seo_score": None, "seo_issues": {},
}

# All fields that constitute a reachable contact channel (phone is scraped from Maps but not
# returned by analyze(), so it's intentionally excluded here)
_CONTACT_FIELDS = ("website", "email", *SOCIAL_DOMAINS)


def is_contactable(lead: dict) -> bool:
    """Return True if the lead has a website, email, or social profile.

    Note: phone is not returned by analyze() — callers with access to the full
    lead record (e.g. the worker's job dict) should check it separately.
    """
    return any(lead.get(f, "").strip() for f in _CONTACT_FIELDS)


def analyze(lead: dict) -> dict:
    """Enrich a lead with CMS, contact, social, and SEO data from its website.

    Args:
        lead: Dict with at least a ``website`` key.

    Returns:
        The lead dict extended with ``cms``, ``email``, per-platform social URLs,
        ``seo_score``, and ``seo_issues``. Leads without a reachable website still
        receive all keys, populated with empty/zero defaults.
    """
    url = lead.get("website", "")
    if not url:
        return {**lead, **_EMPTY_ANALYSIS}

    platform = _identify_platform(url)
    if platform:
        return {**lead, "website": "", **_EMPTY_ANALYSIS, platform: url}

    result = _fetch(url, log_failure=True)
    if not result:
        return {**lead, **_EMPTY_ANALYSIS, "cms": "unreachable"}

    html, soup = result
    cms = _detect_cms(html)
    seo_score, seo_issues = _score_seo(soup, url)

    return {
        **lead,
        "cms": cms,
        "email": _extract_email(soup, url),
        **_extract_socials(soup),
        "seo_score": seo_score,
        "seo_issues": {k: SEO_ISSUE_LABELS.get(k, k) for k in seo_issues},
    }
