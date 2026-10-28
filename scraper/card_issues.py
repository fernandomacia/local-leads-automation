"""What a Google Maps card is missing, as arguments for the call.

A leaf module for the same reason as ``social`` and ``domains``: the reading is taken by
the worker at the ingest and recorded by the audit against the card it was taken from, so
both need it and neither should need Playwright to have it.

A dict in, a dict out: no network, no browser, no state. That is what lets one call fill
the API's payload and the card's audit row without reading the listing twice.
"""

from config import SOCIAL_NAMES


# Maps card fields the AI can use as pitch arguments — parallel to SEO_ISSUE_LABELS
# in web_analyzer.py. Issue key -> (job field that must be populated, pitch label).
# Both halves live in one entry so an issue cannot be declared with a field and no
# label, or with a label the detector never produces.
MAPS_ISSUES: dict[str, tuple[str, str]] = {
    "no_website": ("website", "Sin sitio web en Google Maps"),
    "no_phone":   ("phone",   "Sin teléfono en Google Maps"),
    "no_address": ("address", "Sin dirección en Google Maps"),
}

# The website button can be pointed at the wrong place rather than left empty, and then
# it is this finding and never `no_website`: the business may well have a site. Nobelia
# Textil did — its card sent anyone pressing "Sitio web" to LinkedIn, the card was
# recorded as complete, and the strongest line the agent had went unsaid.
#
# Kept out of MAPS_ISSUES because its label names the network, which no missing field does.
SOCIAL_AS_WEBSITE = "social_as_website"
_SOCIAL_AS_WEBSITE_LABEL = "La ficha de Google Maps enlaza a {network} en lugar de a su web"


def _card_platform(lead: dict) -> str | None:
    """Return the network the card's website button pointed at, if it pointed at one."""
    return next(iter(lead.get("social_networks") or {}), None)


def maps_card_issues(lead: dict) -> dict[str, str]:
    """Return what this business's Google Maps card is missing.

    Read here, from the scraped card, and reported with the ingest batch — never
    re-derived later. The analysis step used to compute it from the job the API handed
    back, which loses the one finding it exists for: an agent supplies a website through
    the panel for a business whose card had none, the worker then sees a job *with* a
    website, and "no website on the listing" silently becomes "the listing was complete".
    The overwrite was invisible because it was a well-formed reading of the wrong thing.

    An empty dict is a real answer — the card is complete — and is what makes NULL on the
    API side mean "no worker has reported on this lead", so callers must send it as it
    comes rather than omitting it when empty.

    ``no_website`` and ``social_as_website`` can never both appear: a button pointing at
    a social profile is a wrong destination, not a missing one, and the business may have
    a website all the same.
    """
    issues = {}
    for key, (field, label) in MAPS_ISSUES.items():
        # Exclusive by construction rather than by the caller remembering the rule: the
        # social profile replaces the missing-website finding, never joins it.
        if key == "no_website" and (platform := _card_platform(lead)):
            issues[SOCIAL_AS_WEBSITE] = _SOCIAL_AS_WEBSITE_LABEL.format(
                network=SOCIAL_NAMES.get(platform, platform))
        elif not lead.get(field):
            issues[key] = label
    return issues
