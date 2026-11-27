"""Finding a business's Google Maps card from its website: what to search, and what to accept.

A lead added by hand arrives with a website and no card, the reverse of a search's leads.
The site says who the business is (its name, phone and town) and Maps is searched with that,
but a search by name returns whatever Google thinks is closest — a namesake in another
province, a competitor, a directory. So a card is accepted only on evidence that ties it to
this site: its website button leads to the same domain, or it lists the same phone. A wrong
card is worse than none, because the pitch would name gaps in somebody else's listing.

Pure functions, no network and no browser, like ``card_issues``: the browser half lives in
``maps_scraper.find_maps_card`` and the fetch in ``web_analyzer.site_identity``.
"""

import re

from bs4 import BeautifulSoup

from scraper.domains import normalize_domain

# Title segments that name the page, not the business ("Inicio | Clínica Sanz").
_GENERIC_TITLES = {
    "inicio", "home", "portada", "bienvenido", "bienvenidos", "página principal",
    "pagina principal", "página de inicio", "web oficial", "sitio web oficial",
}
_TITLE_SEPARATORS = re.compile(r"\s+[|\-–—·•:]\s+")

# Spanish numbers: an optional +34/0034, then nine digits starting 6, 7, 8 or 9, grouped
# however the site likes. The key compared is those nine digits.
_PHONE = re.compile(r"(?:\+34|0034)?[\s.-]*([6789](?:[\s.-]*\d){8})\b")

# "46110 Godella" — a postal code followed by the town, as an address footer writes it.
_POSTAL_TOWN = re.compile(r"\b\d{5}\s+([A-ZÁÉÍÓÚÑ][\wÀ-ÿ'’.-]*(?:\s+(?:de|del|la|las|los|el|d'|i|y)?\s*[A-ZÁÉÍÓÚÑ][\wÀ-ÿ'’.-]*)*)")

_MAX_PHONES = 3


def phone_key(phone: str) -> str:
    """Return the last nine digits of a phone number, the part that identifies it in Spain."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-9:] if len(digits) >= 9 else ""


def _site_name(soup: BeautifulSoup) -> str:
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and (content := (og.get("content") or "").strip()):
        return content

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    segments = [s.strip() for s in _TITLE_SEPARATORS.split(title) if s.strip()]
    named = [s for s in segments if s.lower() not in _GENERIC_TITLES]
    return (named or segments or [""])[0]


def _site_phones(soup: BeautifulSoup) -> list[str]:
    # tel: links first: a number the owner made dialable is the one they answer.
    found = [a["href"][4:] for a in soup.select('a[href^="tel:"]')]
    found += [m.group(0) for m in _PHONE.finditer(soup.get_text(" "))]

    keys: list[str] = []
    for raw in found:
        if (key := phone_key(raw)) and key not in keys:
            keys.append(key)
    return keys[:_MAX_PHONES]


def _site_town(soup: BeautifulSoup) -> str:
    match = _POSTAL_TOWN.search(soup.get_text(" "))
    return match.group(1).strip(" .,") if match else ""


def identity_from_soup(soup: BeautifulSoup) -> dict:
    """Read who a website says it is: its name, its phones (as nine-digit keys) and its town.

    Any of the three may come back empty; the lookup searches with what there is.
    """
    return {"name": _site_name(soup), "phones": _site_phones(soup), "city": _site_town(soup)}


def search_queries(website: str, identity: dict) -> list[str]:
    """Return what to type into Maps, most precise first: the domain, then name and town.

    The domain alone often lands straight on the card when the card links to it; the name
    is the fallback for a card whose website button is empty or points elsewhere.
    """
    queries = [normalize_domain(website) or ""]
    name = identity.get("name") or ""
    if name:
        queries.append(f"{name} {identity.get('city') or ''}".strip())
    return [q for i, q in enumerate(queries) if q and q not in queries[:i]]


def card_matches(card: dict, website: str, phones: list[str]) -> bool:
    """Whether a Maps card belongs to this website: same domain, or one of its phones.

    A social profile in the card's website button does not count as the domain, but the
    phone can still tie the card to the site.
    """
    site_domain = normalize_domain(website)
    card_domain = normalize_domain(card.get("website") or "")
    if site_domain and card_domain == site_domain:
        return True
    return bool(phones) and phone_key(card.get("phone") or "") in phones
