"""Text normalization, word-boundary matching, and URL slug matching.

Three rules the previous substring-based implementation got wrong:

1. **Fold before comparing.** ``Política`` and ``politica`` are the same word to
   a reader and must be the same word here.
2. **Match on word boundaries.** ``"legal" in "/asesoria-legal"`` is true and
   meaningless — a law firm's services page is not a legal notice.
3. **Match slugs against path segments**, never against the whole URL, for the
   same reason.
"""

import re
import unicodedata
from functools import lru_cache
from urllib.parse import urlparse

# Typographic characters that carry no meaning for matching but break equality:
# a banner rendered with a curly apostrophe would miss ``j'accepte``.
_PUNCTUATION_FOLD = str.maketrans({"’": "'", "‘": "'", "´": "'", "`": "'", "–": "-", "—": "-"})

# Word separators inside a term. A site writing "Cookie Richtlinie" instead of
# "Cookie-Richtlinie" is the same document.
_SEPARATOR = r"[\s\-_]+"

# Path suffixes to drop before comparing a segment to a slug.
_PAGE_EXTENSIONS = (".html", ".htm", ".php", ".asp", ".aspx", ".jsp")

# A BCP-47 primary subtag. Anything else in a lang attribute is markup that never
# rendered ("{{ site.lang }}") or a word where a code belongs ("español"), and the
# API rejects it — which costs the lead its whole analysis, not just its language.
_LANGUAGE_SUBTAG = re.compile(r"^[a-z]{2,8}$")

# Minimum slug length allowed to match as a bare prefix. German and Dutch build
# compounds without a separator ("datenschutzerklärung"), so a prefix rule is
# needed — but only for slugs long enough that the match cannot be accidental.
_COMPOUND_PREFIX_MIN = 8


def normalize(text: str) -> str:
    """Fold case, accents, and typographic punctuation for comparison.

    Applied identically to both sides of every comparison, so scripts that
    decompose differently (Cyrillic, Greek) simply fold a little more
    aggressively without ever producing a mismatch.
    """
    folded = text.casefold().translate(_PUNCTUATION_FOLD)
    return "".join(c for c in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(c))


@lru_cache(maxsize=256)
def _pattern(terms: tuple[str, ...]) -> re.Pattern:
    """Compile one alternation matching any term on word boundaries.

    Lookarounds rather than ``\\b`` so terms ending in punctuation still anchor
    correctly. Longest first, so the pattern reports the most specific match.
    """
    alts = sorted(
        (_SEPARATOR.join(re.escape(p) for p in re.split(_SEPARATOR, normalize(t)) if p) for t in terms),
        key=len, reverse=True,
    )
    return re.compile(rf"(?<!\w)(?:{'|'.join(alts)})(?!\w)")


def contains_term(normalized_text: str, terms: tuple[str, ...]) -> bool:
    """True when any term appears as a whole word. Text must already be normalized."""
    return bool(terms) and bool(_pattern(terms).search(normalized_text))


def match_language(
    normalized_text: str,
    terms_by_language: dict[str, tuple[str, ...]],
    preferred: str = "",
) -> str | None:
    """Return the language whose terms appear in the text, or None.

    ``preferred`` (the site's declared language) is tried first so a page whose
    wording is ambiguous across languages — "aviso legal" is both Spanish and
    Portuguese — is attributed to the one the site actually declares. Every
    other language is still tried: restricting the search to the declared
    language is what makes a German site report as having no legal notice.
    """
    order = ([preferred] if preferred in terms_by_language else []) + [
        lang for lang in terms_by_language if lang != preferred
    ]
    return next((lang for lang in order if contains_term(normalized_text, terms_by_language[lang])), None)


def is_navigable(href: str) -> bool:
    """True when the href points at a page rather than at a script or an action.

    Consent banners render their own "Aceptar cookies" control as an anchor;
    counting it would report a policy page that does not exist.
    """
    href = href.strip()
    return bool(href) and not href.lower().startswith(("#", "javascript:", "mailto:", "tel:", "data:"))


def path_segments(href: str) -> list[str]:
    """Return the normalized path segments of a URL, extensions stripped."""
    path = urlparse(href.strip()).path
    segments = []
    for raw in path.split("/"):
        segment = normalize(raw)
        for ext in _PAGE_EXTENSIONS:
            segment = segment.removesuffix(ext)
        if segment:
            segments.append(re.sub(r"[^a-z0-9]+", "-", segment).strip("-"))
    return segments


def matches_slug(href: str, slugs: tuple[str, ...]) -> bool:
    """True when any path segment of the URL is one of the slugs.

    A segment matches a slug when it equals it, extends it with a separator
    ("privacy" → "privacy-policy"), or — for long slugs only — extends it as a
    compound ("datenschutz" → "datenschutzerklarung").
    """
    return any(
        segment == slug
        or segment.startswith(f"{slug}-")
        or (len(slug) >= _COMPOUND_PREFIX_MIN and segment.startswith(slug))
        for segment in path_segments(href)
        for slug in slugs
    )


def _subtag(value: str) -> str:
    """Reduce a language tag to its primary subtag, or "" if it is not one."""
    subtag = value.strip().lower().replace("_", "-").split("-")[0]
    return subtag if _LANGUAGE_SUBTAG.match(subtag) else ""


def detect_language(soup) -> str:
    """Return the site's declared language as a primary subtag, or "".

    Read from ``html[lang]``, then ``og:locale``, then the first ``hreflang``,
    taking the first that is actually a language tag: themes ship unrendered
    template variables and whole words in that attribute often enough that
    passing the value through unchecked is how a lead gets rejected downstream.

    Used to prioritize a lexicon (never to restrict it) and to tell the message
    generator which language to write the outreach email in.
    """
    candidates = (
        (html.get("lang") if (html := soup.find("html")) else None),
        (meta.get("content") if (meta := soup.find("meta", attrs={"property": "og:locale"})) else None),
        (link.get("hreflang") if (link := soup.find("link", attrs={"hreflang": True})) else None),
    )
    return next((subtag for value in candidates if value and (subtag := _subtag(value))), "")
