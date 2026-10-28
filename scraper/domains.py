"""Reducing a URL to the domain the API stores.

A leaf module for the same reason as ``social`` and ``net_guard``: the Maps scraper asks
which domain a card's website belongs to, and the audit recorder asks the same of a card
it is filing, so joining the two by an import would drag Chromium into a package that
needs one function. The normalisation is a port of the API's ``App\\Values\\Domain`` and
has to stay one — see ``normalize_domain``.
"""

from urllib.parse import urlparse

import idna


def to_ascii(host: str) -> str:
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
    the check is an optimisation, and the API normalises what it is *sent* at ingest, so
    deduplication there is unaffected. See ``TestNormalizeDomainAgreesWithTheApi``.
    """
    if not host or host.isascii():
        return host

    try:
        return idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError:
        return host


def normalize_domain(url: str) -> str:
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

    return to_ascii(host.rstrip(".").lower().removeprefix("www."))
