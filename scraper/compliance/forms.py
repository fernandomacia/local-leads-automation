"""Consent collection in contact forms (GDPR arts. 6, 7 and 13).

Three findings, in descending severity:

* a pre-ticked consent checkbox — consent that is legally void (CJEU C-673/17,
  *Planet49*), and worse than none, because the site believes it is covered;
* a form with neither checkbox nor privacy link — data collected with no
  information and no consent at all;
* a form that links the privacy policy but never asks — informed, not consented.
"""

from .matching import contains_term, is_navigable, matches_slug, normalize
from .vocabulary import CONSENT_TERMS, PRIVACY_SLUGS, PRIVACY_TERMS

# Input types that collect personal data. Submit, hidden, button and the rest are
# excluded so they never push a form over the field threshold below.
_DATA_INPUT_TYPES = (None, "text", "email", "tel")

# A form must collect at least this many fields to read as a contact form. A site
# search box is a single text input and would otherwise be reported as a form
# gathering personal data without consent.
_MIN_CONTACT_FIELDS = 2

_ALL_PRIVACY_TERMS = tuple(t for terms in PRIVACY_TERMS.values() for t in terms)


def _links_privacy(form) -> bool:
    """True when the form links to the privacy policy, by slug or by link text."""
    return any(
        is_navigable(a["href"])
        and (matches_slug(a["href"], PRIVACY_SLUGS)
             or contains_term(normalize(a.get_text(" ", strip=True)), _ALL_PRIVACY_TERMS))
        for a in form.find_all("a", href=True)
    )


def _is_consent_checkbox(checkbox) -> bool:
    """True when the checkbox asks for consent rather than a newsletter or a preference.

    Reads its own attributes and the label wrapping it — the visible wording
    ("acepto la política de privacidad") is where the intent actually lives.
    """
    context = " ".join(str(v) for v in checkbox.attrs.values())
    if label := checkbox.find_parent("label"):
        context += " " + label.get_text(" ", strip=True)
    normalized = normalize(context)
    return contains_term(normalized, CONSENT_TERMS) or contains_term(normalized, _ALL_PRIVACY_TERMS)


def _audit_form(form) -> str | None:
    """Return the issue key for one form, or None when it collects consent correctly."""
    inputs = form.find_all(("input", "textarea"))
    if sum(1 for i in inputs if i.get("type") in _DATA_INPUT_TYPES) < _MIN_CONTACT_FIELDS:
        return None

    checkboxes = [i for i in inputs if i.get("type") == "checkbox"]
    if any(c.has_attr("checked") and _is_consent_checkbox(c) for c in checkboxes):
        return "form_consent_prechecked"
    if checkboxes:
        return None
    return "form_consent_link_only" if _links_privacy(form) else "form_without_consent"


def detect_form_consent(*soups) -> list[str]:
    """Return issue keys for contact forms that gather personal data without consent.

    Takes several soups because the contact form usually lives on /contacto rather
    than the homepage: pass any contact sub-pages that were already fetched. One
    offending form anywhere is enough to report, and a single site can hit more
    than one finding across its forms.
    """
    found = {_audit_form(form) for soup in soups for form in soup.find_all("form")} - {None}
    # Ordered by severity so the generated pitch leads with the strongest finding.
    return [k for k in ("form_consent_prechecked", "form_without_consent", "form_consent_link_only")
            if k in found]
