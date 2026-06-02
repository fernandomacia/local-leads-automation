import re
import urllib3
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 10
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

SOCIAL_DOMAINS = {
    "instagram": "instagram.com",
    "facebook": "facebook.com",
    "youtube": "youtube.com",
    "linkedin": "linkedin.com",
    "twitter": "twitter.com",
    "tiktok": "tiktok.com",
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
]

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_CONTACT_PATHS = ("/contacto", "/contact", "/contactar")


def _fetch(url: str) -> tuple[str, BeautifulSoup] | None:
    """Fetch URL and return (raw_html, soup). Returns None on any failure."""
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS, allow_redirects=True, verify=False)
        resp.raise_for_status()
        return resp.text, BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return None


def _is_social_url(url: str) -> bool:
    """Return True if the URL points directly to a social media platform."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == domain or host.endswith("." + domain) for domain in SOCIAL_DOMAINS.values())


def _detect_cms(html: str) -> str:
    for cms, signatures in CMS_SIGNATURES:
        if any(sig in html for sig in signatures):
            return cms
    return "unknown"


def _extract_email(soup: BeautifulSoup, base_url: str) -> str:
    # mailto link on current page
    mailto = soup.find("a", href=re.compile(r"^mailto:", re.I))
    if mailto:
        return mailto["href"][7:].split("?")[0].strip()

    # email regex in page text
    emails = _EMAIL_RE.findall(soup.get_text())
    if emails:
        return emails[0]

    # fallback: try contact pages
    for path in _CONTACT_PATHS:
        result = _fetch(urljoin(base_url, path))
        if not result:
            continue
        _, contact_soup = result
        mailto = contact_soup.find("a", href=re.compile(r"^mailto:", re.I))
        if mailto:
            return mailto["href"][7:].split("?")[0].strip()
        emails = _EMAIL_RE.findall(contact_soup.get_text())
        if emails:
            return emails[0]

    return ""


def _extract_socials(soup: BeautifulSoup) -> dict[str, str]:
    found = {p: "" for p in SOCIAL_DOMAINS}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        for platform, domain in SOCIAL_DOMAINS.items():
            if not found[platform] and domain in href:
                found[platform] = href
    return found


def _url_exists(url: str) -> bool:
    try:
        resp = requests.head(url, timeout=5, headers=HEADERS, allow_redirects=True, verify=False)
        return resp.status_code < 400
    except Exception:
        return False


def _score_seo(soup: BeautifulSoup, url: str) -> tuple[int, list[str]]:
    issues = []
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # Security
    if not url.startswith("https://"):
        issues.append("no_https")

    # On-page SEO
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
    if any(img for img in soup.find_all("img") if not img.get("alt")):
        issues.append("no_alt_images")

    # Analytics — check for GA4, GTM, Universal Analytics
    html_str = str(soup)
    if not any(s in html_str for s in ("gtag(", "googletagmanager.com", "google-analytics.com", "_gaq")):
        issues.append("no_analytics")

    # Favicon
    if not soup.find("link", rel=lambda r: isinstance(r, list) and "icon" in r or r == "icon"):
        issues.append("no_favicon")

    # Crawlability (one extra HEAD request each)
    if not _url_exists(f"{base_url}/sitemap.xml"):
        issues.append("no_sitemap")
    if not _url_exists(f"{base_url}/robots.txt"):
        issues.append("no_robots")

    return max(0, 100 - len(issues) * 10), issues


_EMPTY: dict = {
    "cms": "", "email": "",
    **{p: "" for p in SOCIAL_DOMAINS},
    "seo_score": 0, "seo_issues": "",
}


def analyze(lead: dict) -> dict:
    """Fetch and analyze a lead's website, returning the lead enriched with web data.

    Args:
        lead: Dict with at least a 'website' key.

    Returns:
        The lead dict extended with cms, email, social URLs, seo_score, seo_issues.
    """
    url = lead.get("website", "")
    if not url or _is_social_url(url):
        return {**lead, **_EMPTY}

    result = _fetch(url)
    if not result:
        return {**lead, **_EMPTY, "cms": "unreachable"}

    html, soup = result
    seo_score, seo_issues = _score_seo(soup, url)

    return {
        **lead,
        "cms": _detect_cms(html),
        "email": _extract_email(soup, url),
        **_extract_socials(soup),
        "seo_score": seo_score,
        "seo_issues": "|".join(seo_issues),
    }
