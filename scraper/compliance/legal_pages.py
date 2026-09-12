"""Resolution and verification of the three mandatory legal documents.

Each document — legal notice, privacy policy, cookie policy — is resolved to
one of the statuses reported in ``compliance_details``:

``ok``
    Linked, reachable, and carrying the content the law requires.
``incomplete``
    The page exists but omits mandatory content (see ``missing_items``).
``broken_link``
    The footer links it, the link 404s.
``unlinked``
    The page exists at a known path but nothing links to it — the LSSI and the
    GDPR both require the texts to be directly accessible.
``missing``
    Neither linked nor found at any known path.
``not_applicable``
    The obligation does not apply (cookie policy on a site with no non-exempt
    cookies).
``unknown``
    The request budget ran out, or the server stopped answering. Reported as no
    finding at all: not knowing is not evidence of breaching.

Links are matched on visible text in every supported language *and* on URL
slugs, because multilingual sites routinely keep one while translating the other.
"""

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .matching import contains_term, is_navigable, match_language, matches_slug, normalize
from .page_content import is_substantive, missing_requirements
from .vocabulary import DOCUMENT_SLUGS, DOCUMENT_TERMS, PROBE_PATHS

DOCUMENTS = tuple(DOCUMENT_TERMS)


class RequestBudget:
    """Hard cap on the requests one lead's compliance audit may issue.

    Verification is worth a few requests per lead and no more: the analyser runs
    against thousands of leads and the pages it probes belong to businesses that
    never asked to be scanned.
    """

    def __init__(self, limit: int):
        self.remaining = limit

    def spend(self) -> bool:
        """Consume one request, returning False when the budget is exhausted."""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _find_link(soups, document: str, language: str) -> tuple[str, str] | None:
    """Return ``(href, matched_language)`` for the best link to the document.

    Candidates are ranked, because the broader terms ("protección de datos",
    "privacidad") also appear in ordinary content: on aepd.es they matched a
    press release about home CCTV before the actual policy. A link whose URL
    *and* wording both name the document wins over either on its own.
    """
    terms, slugs = DOCUMENT_TERMS[document], DOCUMENT_SLUGS[document]
    best = None
    for soup in soups:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not is_navigable(href):
                continue
            matched = match_language(normalize(a.get_text(" ", strip=True)), terms, language)
            by_slug = matches_slug(href, slugs)
            if matched and by_slug:
                return href, matched
            rank = 2 if by_slug else 1 if matched else 0
            if rank and (best is None or rank > best[0]):
                best = (rank, href, matched or language)
    return (best[1], best[2]) if best else None


def find_links(soups, language: str = "") -> dict[str, tuple[str, str] | None]:
    """Resolve every document to its best link across all pages already fetched."""
    return {document: _find_link(soups, document, language) for document in DOCUMENTS}


def _page_text(html: str) -> str:
    """Visible text of a fetched page, scripts and styles excluded."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(("script", "style", "noscript")):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _audit_content(document: str, html: str, language: str, profession: str) -> tuple[str, list[str]]:
    """Classify a fetched legal page as ``ok``, ``incomplete``, or an empty template."""
    text = _page_text(html)
    if not is_substantive(text):
        # A theme placeholder or a page holding only a heading. Treating it as
        # published would be the analyser's most easily disproved finding.
        return "empty", []
    if not contains_term(normalize(text), tuple(t for terms in DOCUMENT_TERMS[document].values() for t in terms)):
        return "empty", []
    missing = missing_requirements(document, text, language, profession)
    return ("incomplete", missing) if missing else ("ok", [])


def _verify_linked(document, href, language, base_url, fetch, budget, profession) -> dict:
    """Fetch a linked document and classify what came back."""
    url = urljoin(base_url, href)
    detail = {"found_url": url, "method": "link", "language": language, "checked_urls": [url]}

    if not budget.spend():
        # The link exists; we simply never got to read it. Reporting anything
        # worse than "published" would be a guess against the evidence we have.
        return {**detail, "status": "ok", "validated": False}

    # GET, never HEAD: a large share of WordPress installs answer 405 to HEAD.
    response = fetch(url)
    if response is None:
        return {**detail, "status": "unknown", "reason": "unreachable"}
    status_code, html = response
    if status_code >= 400:
        return {**detail, "status": "broken_link", "http_status": status_code}
    if html is None:
        # The page answers but its body is not HTML we can read — a PDF legal
        # notice, most often. Published, just not verifiable.
        return {**detail, "status": "ok", "validated": False}

    verdict, missing = _audit_content(document, html, language, profession)
    if verdict == "empty":
        return {**detail, "status": "missing", "reason": "empty_page"}
    if verdict == "incomplete":
        return {**detail, "status": "incomplete", "missing_items": missing, "validated": True}
    return {**detail, "status": "ok", "validated": True}


def _probe(document, base_url, language, fetch, budget, profession, max_probes: int) -> dict:
    """Look for an unlinked document at the paths it conventionally lives at."""
    if max_probes <= 0:
        return {"status": "unknown", "reason": "budget_exhausted", "checked_urls": [],
                "found_url": None, "method": "probe"}

    checked, answered = [], False
    for path in PROBE_PATHS[document][:max_probes]:
        if not budget.spend():
            # Out of budget mid-probe: we have not seen enough to claim the
            # document does not exist.
            return {"status": "unknown", "reason": "budget_exhausted", "checked_urls": checked,
                    "found_url": None, "method": "probe"}
        url = urljoin(base_url, path)
        checked.append(url)
        response = fetch(url)
        if response is None:
            continue
        answered = True
        status_code, html = response
        if status_code >= 400 or html is None:
            continue
        verdict, missing = _audit_content(document, html, language, profession)
        if verdict == "empty":
            continue
        detail = {"status": "unlinked", "found_url": url, "method": "probe",
                  "language": language, "checked_urls": checked, "validated": True}
        return {**detail, "missing_items": missing} if missing else detail

    if not answered:
        # Every probe failed at the network level: the server stopped answering
        # mid-audit, which says nothing about the document.
        return {"status": "unknown", "reason": "unreachable", "checked_urls": checked,
                "found_url": None, "method": "probe"}
    return {"status": "missing", "found_url": None, "method": "probe", "checked_urls": checked}


def verify_documents(links, base_url: str, *, fetch=None, budget=None,
                     language: str = "", profession: str = "", skip=()) -> dict[str, dict]:
    """Turn resolved links into a ``compliance_details`` entry per document.

    Without ``fetch`` the audit stays static: a document is ``ok`` when a link
    to it exists and ``missing`` when none does. With ``fetch``, every link is
    followed and every unlinked document is probed, within ``budget``.

    Args:
        links: Output of :func:`find_links`.
        base_url: Final homepage URL, used to absolutize hrefs and build probes.
        fetch: ``(url) -> (status_code, html) | None``. None means the request
            failed at the network level, which is distinct from a 404.
        budget: Shared :class:`RequestBudget` for this lead.
        language: Site language, used to prioritize the lexicon and to decide
            whether the Spanish tax-id requirement applies.
        profession: Search term the lead came from, for the regulated-profession
            requirements of LSSI art. 10.1.c.
        skip: Documents whose obligation does not apply, left to the caller to
            describe — they are not requested at all.
    """
    documents = [d for d in DOCUMENTS if d not in skip]
    details = {}

    if fetch is None:
        return {document: (
            {"status": "ok", "found_url": urljoin(base_url, links[document][0]), "method": "link",
             "language": links[document][1] or language}
            if links.get(document) else
            {"status": "missing", "found_url": None, "method": "link"})
            for document in documents}

    # Linked documents first: one request each, and the answer is worth more than
    # any probe. Whatever budget survives is then split evenly among the documents
    # that have to be probed — spending it all on the first would leave the cookie
    # policy permanently "unknown" on exactly the sites that publish nothing.
    for document in documents:
        if link := links.get(document):
            details[document] = _verify_linked(
                document, link[0], link[1] or language, base_url, fetch, budget, profession)

    to_probe = [d for d in documents if d not in details]
    for position, document in enumerate(to_probe):
        allowance = budget.remaining // (len(to_probe) - position)
        details[document] = _probe(
            document, base_url, language, fetch, budget, profession, allowance)

    return {document: details[document] for document in documents}
