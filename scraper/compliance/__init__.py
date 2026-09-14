"""Legal-compliance analysis of a lead's website (LSSI, RGPD, cookie rules).

One entry point, :func:`detect_compliance`, which orchestrates the specialised
modules and returns both a customer-facing issue list and the evidence behind
it. The audit works on HTML the analyzer already fetched and, when handed a
``fetch`` callable, verifies each legal page actually exists and says what the
law requires — within a hard per-lead request budget.

The whole package is deliberately conservative. Every finding is read out loud
to a business owner as "you are breaking the law", so anything ambiguous
resolves to "compliant": a check that cannot see enough to be sure reports
nothing rather than guessing.
"""

from bs4 import BeautifulSoup

from config import MAX_COMPLIANCE_REQUESTS

from .cmp import has_cmp, has_generic_banner, reject_status
from .forms import detect_form_consent
from .legal_pages import DOCUMENTS, RequestBudget, find_links, verify_documents
from .matching import detect_language
from .trackers import loads_trackers
from .vocabulary import DOCUMENT_TERMS, PROBE_PATHS, REQUIREMENT_LABELS

__all__ = ["COMPLIANCE_ISSUE_LABELS", "detect_compliance"]

# How many languages each document is searched in. Interpolated into the labels
# so the claim made to the customer cannot drift from the lexicon behind it.
_LANGUAGE_COUNT = len(set().union(*DOCUMENT_TERMS.values()))

# Subject and governing article per document, so the twelve document labels stay
# consistent with each other and with the checks that produce them.
_DOCUMENT_LABELS: dict[str, tuple[str, str, str]] = {
    "legal_notice":   ("aviso legal", "El aviso legal", "LSSI art. 10"),
    "privacy_policy": ("política de privacidad", "La política de privacidad", "RGPD art. 13"),
    "cookie_policy":  ("política de cookies", "La política de cookies", "RGPD/LSSI art. 22"),
}

# Document status → issue key suffix. "missing" keeps its original ``no_*`` key:
# the platform's filter dropdown is built on these, and renaming would drop the
# findings that already exist in the database.
_STATUS_ISSUES = {"missing": None, "broken_link": "broken",
                  "unlinked": "unlinked", "incomplete": "incomplete"}


def _document_labels() -> dict[str, str]:
    """Build the label for every document finding, stating what was checked."""
    labels = {}
    for document, (name, subject, law) in _DOCUMENT_LABELS.items():
        routes = len(PROBE_PATHS[document])
        labels[f"no_{document}"] = (
            f"Sin {name}: no está enlazada ni existe en las rutas habituales "
            f"({law}; comprobado en {_LANGUAGE_COUNT} idiomas y {routes} rutas)")
        labels[f"{document}_broken"] = f"{subject} está enlazado pero el enlace está roto ({law})"
        labels[f"{document}_unlinked"] = (
            f"{subject} existe pero no está enlazado desde la web: la ley exige acceso "
            f"directo y permanente ({law})")
        labels[f"{document}_incomplete"] = f"{subject} no incluye todos los datos obligatorios ({law})"
    return labels


# Adding a key here? The panel renders these labels as they arrive, so the lead card and
# the list column pick it up with no frontend change. Declare it in the API's
# ComplianceIssue enum too, which serves the filter dropdown its key universe.
COMPLIANCE_ISSUE_LABELS: dict[str, str] = {
    **_document_labels(),
    "no_cookie_banner":
        "Sin aviso ni gestor de cookies, con cookies no exentas ya cargadas (RGPD/LSSI art. 22)",
    # The most sanctioned cookie infringement and the easiest to fix: rejecting must
    # be as easy as accepting, in the first layer.
    "cookie_banner_without_reject":
        "Banner de cookies sin opción de rechazo en la primera capa (Guía de cookies AEPD 2023)",
    "form_without_consent":
        "Formulario de contacto que recoge datos sin consentimiento ni información de "
        "privacidad (RGPD arts. 6 y 13)",
    "form_consent_link_only":
        "Formulario que enlaza la política de privacidad pero no recoge consentimiento "
        "expreso (RGPD art. 7)",
    "form_consent_prechecked":
        "Casilla de consentimiento premarcada: el consentimiento no es válido (TJUE C-673/17)",
}


def _issue_key(document: str, status: str) -> str | None:
    """Return the issue key for a document status, or None when it is no finding."""
    if status not in _STATUS_ISSUES:
        return None
    suffix = _STATUS_ISSUES[status]
    return f"{document}_{suffix}" if suffix else f"no_{document}"


def _label(key: str, detail: dict) -> str:
    """Return the customer-facing label, naming the specific gaps when known."""
    label = COMPLIANCE_ISSUE_LABELS.get(key, key)
    if missing := detail.get("missing_items"):
        named = ", ".join(REQUIREMENT_LABELS.get(item, item) for item in missing)
        return f"{label}: faltan {named}"
    return label


def detect_compliance(html: str, soup, base_url: str, extra_soups=(), *,
                      fetch=None, render=None, profession: str = "") -> dict:
    """Audit a website's legal compliance.

    Args:
        html: Raw homepage HTML, used for the tracker and CMP loader signatures.
        soup: Parsed homepage.
        base_url: Final homepage URL after redirects, used to absolutize links.
        extra_soups: Any sub-pages already fetched (typically /contacto), reused
            for the form check and for footers that only render on inner pages.
        fetch: ``(url) -> (status_code, html) | None``, used to verify that each
            legal page exists and says what it must. Without it the audit stays
            static and only reports documents nothing links to.
        render: ``(url) -> html | None``, a browser fallback called only when no
            legal link of any language is found in the static HTML.
        profession: The search term the lead came from, for the extra legal-notice
            requirements that apply to regulated professions.

    Returns:
        ``{"compliance_issues": {key: label}, "compliance_details": {...}}`` —
        the issues as the customer reads them, and the evidence per document so
        a finding can be defended when the owner disputes it.
    """
    html_lower = html.lower()
    soups = [soup, *extra_soups]
    language = detect_language(soup)
    links = find_links(soups, language)
    banner_soup = soup

    # A footer built client-side is the main source of false "no legal notice"
    # findings. Having paid for a browser, the rendered DOM replaces the static
    # one for every check, not just for the links.
    if render and not any(links.values()):
        if rendered := render(base_url):
            banner_soup = BeautifulSoup(rendered, "html.parser")
            soups.append(banner_soup)
            html_lower += rendered.lower()
            language = language or detect_language(banner_soup)
            links = find_links(soups, language)

    has_trackers = loads_trackers(html_lower)
    details = verify_documents(
        links, base_url, fetch=fetch, budget=RequestBudget(MAX_COMPLIANCE_REQUESTS),
        language=language, profession=profession,
        # No non-exempt cookies means no consent obligation and no policy to
        # publish, so the document is not even requested.
        skip=() if has_trackers else ("cookie_policy",),
    )
    if not has_trackers:
        details["cookie_policy"] = {"status": "not_applicable", "reason": "no_trackers"}

    issues = {}
    # Both DOMs count: a banner present in the static HTML is not unpublished
    # because the rendered copy came back without it.
    consent_soups = (soup,) if banner_soup is soup else (soup, banner_soup)
    has_consent_ui = any(has_cmp(html_lower, s) or has_generic_banner(s) for s in consent_soups)
    reject = next((r for s in (banner_soup, soup) if (r := reject_status(s)) != "unknown"), "unknown")

    if has_trackers and not has_consent_ui:
        issues["no_cookie_banner"] = COMPLIANCE_ISSUE_LABELS["no_cookie_banner"]
    elif has_consent_ui and reject == "missing":
        issues["cookie_banner_without_reject"] = COMPLIANCE_ISSUE_LABELS["cookie_banner_without_reject"]

    for document in DOCUMENTS:
        detail = details[document]
        if key := _issue_key(document, detail["status"]):
            issues[key] = _label(key, detail)

    issues.update({k: COMPLIANCE_ISSUE_LABELS[k] for k in detect_form_consent(*soups)})

    return {"compliance_issues": issues, "compliance_details": details}
