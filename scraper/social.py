"""Which social network a URL belongs to.

Shared by two callers that ask it for opposite reasons: the Maps scraper, which has to
tell a social profile in the card's website button from an actual website, and the
analyzer, which routes a social-only URL out of the website analysis. A leaf module for
the same reason as ``net_guard`` — the alternative was the scraper importing the whole
analyzer, and with it BeautifulSoup and the compliance package, for one function.
"""

from urllib.parse import urlparse

from config import SOCIAL_DOMAINS


def identify_platform(url: str) -> str | None:
    """Return the platform the URL belongs to, or None when it is not a social profile.

    A subdomain counts as the network itself: ``es.linkedin.com`` is how a Spanish
    company profile is linked, and it is exactly what a Maps card carries.
    """
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if not host:
        return None
    return next((platform for platform, domains in SOCIAL_DOMAINS.items()
                 if any(host == domain or host.endswith(f".{domain}") for domain in domains)),
                None)
