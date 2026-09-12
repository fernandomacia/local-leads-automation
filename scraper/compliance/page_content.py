"""Validation of what a legal page actually says.

The commercial difference between this analyser and a link checker: nearly
every WordPress site has an ``/aviso-legal`` page, and a large share of them
carry the theme's placeholder or a copy-pasted template missing half the
mandatory content. A page that exists is not a page that complies.

Checked against LSSI art. 10 (legal notice), GDPR art. 13 (privacy policy) and
the AEPD cookie guide (cookie policy).
"""

import re

from config import MIN_LEGAL_PAGE_CHARS

from .matching import contains_term, normalize
from .vocabulary import (
    CONTENT_REQUIREMENTS,
    REGULATED_PROFESSIONS,
    SPANISH_LANGUAGES,
)

# Spanish tax ids, with the optional separator legal notices write them with
# ("B-12345678"). The control character is verified below, so an invoice number
# or a year range is never mistaken for a company id.
_TAX_ID_RE = re.compile(r"\b([A-Za-z])?[-\s]?(\d{7,8})[-\s]?([A-Za-z])?\b")
_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_NIE_PREFIXES = {"X": "0", "Y": "1", "Z": "2"}
_CIF_LETTERS = "ABCDEFGHJNPQRSUVW"
_CIF_CONTROL_LETTERS = "JABCDEFGHI"


def _cif_control(digits: str) -> tuple[str, str]:
    """Return the two valid control characters (digit and letter) for a CIF body."""
    odd = sum(sum(divmod(int(d) * 2, 10)) for d in digits[0::2])
    total = odd + sum(int(d) for d in digits[1::2])
    control = (10 - total % 10) % 10
    return str(control), _CIF_CONTROL_LETTERS[control]


def has_tax_id(text: str) -> bool:
    """True when the text contains a structurally valid NIF, NIE or CIF."""
    for prefix, digits, suffix in _TAX_ID_RE.findall(text):
        prefix, suffix = prefix.upper(), suffix.upper()
        if prefix and prefix in _CIF_LETTERS:
            # The control character may be a letter or a digit, and a digit is
            # absorbed into the digit run by the regex.
            if (len(digits) == 7 and suffix in _cif_control(digits)) or (
                    len(digits) == 8 and digits[-1] in _cif_control(digits[:7])):
                return True
        elif prefix in _NIE_PREFIXES and len(digits) == 7:
            if suffix == _DNI_LETTERS[int(_NIE_PREFIXES[prefix] + digits) % 23]:
                return True
        elif not prefix and len(digits) == 8 and suffix == _DNI_LETTERS[int(digits) % 23]:
            return True
    return False


def is_substantive(text: str) -> bool:
    """True when the page carries enough text to be an actual legal document."""
    return len(text.strip()) >= MIN_LEGAL_PAGE_CHARS


def missing_requirements(document: str, text: str, language: str = "", profession: str = "") -> list[str]:
    """Return the mandatory contents the document fails to mention.

    Args:
        document: ``legal_notice``, ``privacy_policy`` or ``cookie_policy``.
        text: Visible text of the page.
        language: Language the document was matched in. The tax id is only
            required for Spanish-state sites; elsewhere the identity of the
            provider is covered by the generic requirements.
        profession: The search term the lead came from. Regulated professions
            must also state their bar association and membership number
            (LSSI art. 10.1.c) — the page itself cannot tell us that, only the
            profession can.

    Returns:
        Requirement keys, in the order they are defined, or ``[]`` when the
        document covers everything.
    """
    normalized = normalize(text)
    missing = [key for key, terms in CONTENT_REQUIREMENTS[document].items()
               if not contains_term(normalized, terms)]

    if document == "legal_notice":
        if (language or "es") in SPANISH_LANGUAGES and not has_tax_id(text):
            missing.insert(0, "tax_id")
        if profession and contains_term(normalize(profession), REGULATED_PROFESSIONS) \
                and not contains_term(normalized, ("colegio", "colegiado", "colegiada", "colegiados",
                                                   "ilustre colegio", "n de colegiado")):
            missing.append("bar_association")

    return missing
