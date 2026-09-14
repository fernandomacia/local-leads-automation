"""Tests for scraper/compliance/ — no network.

Every check in the package runs on HTML that is already in memory, so these
tests are pure fixtures. The bias of the suite mirrors the bias of the package:
the expensive failure is a false positive, because it is read out loud to a
business owner as "you are breaking the law", so most cases here assert that a
compliant site is reported clean.
"""

import pytest
from bs4 import BeautifulSoup

from config import MAX_COMPLIANCE_REQUESTS

from scraper.compliance import detect_compliance
from scraper.compliance.cmp import has_cmp, has_generic_banner, reject_status
from scraper.compliance.forms import detect_form_consent
from scraper.compliance.legal_pages import find_links, verify_documents
from scraper.compliance.matching import (
    detect_language,
    match_language,
    matches_slug,
    normalize,
    path_segments,
)
from scraper.compliance.vocabulary import (
    COOKIE_TERMS,
    LEGAL_NOTICE_SLUGS,
    LEGAL_NOTICE_TERMS,
    PRIVACY_SLUGS,
    PRIVACY_TERMS,
)

BASE = "https://ejemplo.es"

# A consent-gated tracker. Cookie fixtures are paired with one, because without a
# tracker there is no consent obligation and the assertion would hold even if
# detection were broken.
TRACKER = '<script src="https://www.googletagmanager.com/gtag/js?id=G-X"></script>'

# A complete legal footer, so cookie and form fixtures are not polluted by the
# document findings they are not testing.
FOOTER = ('<footer><a href="/aviso-legal">Aviso legal</a>'
          '<a href="/politica-de-privacidad">Política de privacidad</a>'
          '<a href="/politica-de-cookies">Política de cookies</a></footer>')

CONTACT_FIELDS = '<input type="text" name="nombre"><input type="email" name="email">'


def _parse(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _audit(html: str, *extra: str, **kwargs) -> dict:
    """Run the full audit over a homepage and any already-fetched sub-pages."""
    return detect_compliance(html, _parse(html), BASE, [_parse(e) for e in extra], **kwargs)


def _issues(html: str, *extra: str) -> set[str]:
    return set(_audit(html, *extra)["compliance_issues"])


def _documents(html: str, *extra: str) -> dict:
    return _audit(html, *extra)["compliance_details"]


# ── matching ──────────────────────────────────────────────────────────────────

class TestMatching:
    @pytest.mark.parametrize("raw, expected", [
        ("Política", "politica"),
        ("PRYWATNOŚCI", "prywatnosci"),
        ("Confidențialitate", "confidentialitate"),
        ("Evästeet", "evasteet"),
        ("Straße", "strasse"),
        ("J’accepte", "j'accepte"),
    ])
    def test_normalize_folds_case_accents_and_punctuation(self, raw, expected):
        assert normalize(raw) == expected

    def test_word_boundary_stops_the_asesoria_legal_false_positive(self):
        # The case that motivated the rewrite: a law firm's services page was read
        # as its legal notice, so every such lead was silently dropped from the list.
        assert match_language(normalize("Asesoría legal"), LEGAL_NOTICE_TERMS) is None
        assert matches_slug("/servicios/asesoria-legal", LEGAL_NOTICE_SLUGS) is False

    def test_legal_notice_still_matches_when_genuine(self):
        assert match_language(normalize("Aviso Legal"), LEGAL_NOTICE_TERMS) == "es"
        assert matches_slug("/es/aviso-legal/", LEGAL_NOTICE_SLUGS) is True

    @pytest.mark.parametrize("href", [
        "/privacy-policy.html", "/en/privacy", "https://ejemplo.es/datenschutzerklaerung",
        "/politica-de-privacidad/?lang=es",
    ])
    def test_slug_matches_segments_and_compounds(self, href):
        assert matches_slug(href, PRIVACY_SLUGS) is True

    @pytest.mark.parametrize("href", ["/blog/la-privacidad-importa", "/gdprmyths", "/legalidad"])
    def test_slug_does_not_match_unrelated_segments(self, href):
        # A segment that merely *extends* a slug with a separator does match
        # ("privacy" → "privacy-policy"); these do not extend one at all.
        assert matches_slug(href, PRIVACY_SLUGS + LEGAL_NOTICE_SLUGS) is False

    def test_path_segments_strip_extension_and_case(self):
        assert path_segments("https://x.es/ES/Aviso-Legal.php?a=1") == ["es", "aviso-legal"]

    @pytest.mark.parametrize("html, expected", [
        ('<html lang="ca-ES"></html>', "ca"),
        ('<html><meta property="og:locale" content="de_DE"></html>', "de"),
        ('<html><link rel="alternate" hreflang="fr-FR" href="/fr/"></html>', "fr"),
        ("<html></html>", ""),
    ])
    def test_detect_language(self, html, expected):
        assert detect_language(_parse(html)) == expected

    def test_declared_language_breaks_ties_between_lexicons(self):
        # "aviso legal" is Spanish, Galician and Portuguese at once; the site says
        # which one it is, and the message generator needs the right answer.
        assert match_language(normalize("Aviso legal"), LEGAL_NOTICE_TERMS, "pt") == "pt"


# ── legal documents, per language ─────────────────────────────────────────────

class TestDocumentDetection:
    @pytest.mark.parametrize("lang", ["es", "ca", "en", "de", "fr", "it", "nl", "pl", "el", "ru"])
    def test_legal_notice_recognised_in_every_language(self, lang):
        html = f'<a href="/p/1">{LEGAL_NOTICE_TERMS[lang][0]}</a>'
        assert _documents(html)["legal_notice"]["status"] == "ok"

    @pytest.mark.parametrize("lang", ["es", "ca", "en", "de", "fr", "it", "nl", "pl", "fi", "ru"])
    def test_privacy_policy_recognised_in_every_language(self, lang):
        html = f'<a href="/p/1">{PRIVACY_TERMS[lang][0]}</a>'
        assert _documents(html)["privacy_policy"]["status"] == "ok"

    @pytest.mark.parametrize("lang", ["es", "en", "de", "fi", "sv", "hr", "sl", "hu", "bg", "et"])
    def test_cookie_policy_recognised_where_the_word_cookie_does_not_appear(self, lang):
        # fi, sv, hr, sl, hu, bg and et have no "cookie" substring at all, which is
        # precisely what the old `"cookie" in href` filter could never see.
        html = TRACKER + f'<a href="/p/1">{COOKIE_TERMS[lang][0]}</a>'
        assert _documents(html)["cookie_policy"]["status"] == "ok"

    def test_document_found_by_slug_when_the_label_is_generic(self):
        details = _documents('<a href="/impressum">Mehr erfahren</a>')["legal_notice"]
        assert details["status"] == "ok"
        assert details["found_url"] == f"{BASE}/impressum"

    def test_found_url_is_absolute(self):
        assert _documents('<a href="/aviso-legal">Aviso legal</a>')["legal_notice"]["found_url"] \
            == f"{BASE}/aviso-legal"

    def test_language_of_the_match_is_reported(self):
        assert _documents('<html lang="de"><a href="/p/1">Impressum</a></html>'
                          )["legal_notice"]["language"] == "de"

    def test_the_site_language_is_reported_apart_from_the_match(self):
        # A bilingual site: the link matched the German lexicon, but the site declares
        # Spanish. The panel labels this one "the language the web is written in".
        result = _audit('<html lang="es"><a href="/p/1">Impressum</a></html>')
        assert result["compliance_language"] == "es"
        assert result["compliance_details"]["legal_notice"]["language"] == "de"

    def test_the_policy_wins_over_an_article_that_merely_mentions_the_term(self):
        # Real case from aepd.es: a press release about home CCTV matched
        # "protección de datos" and was reported as the privacy policy.
        html = ('<a href="/prensa/videocamaras-y-proteccion-de-datos">Protección de datos</a>'
                '<a href="/politica-de-privacidad">Política de privacidad</a>')
        assert _documents(html)["privacy_policy"]["found_url"] == f"{BASE}/politica-de-privacidad"

    def test_bare_page_reports_both_documents_missing(self):
        assert {"no_legal_notice", "no_privacy_policy"} <= _issues("<html><body><h1>Hola</h1></body></html>")

    @pytest.mark.parametrize("href", ["#", "javascript:void(0)", "mailto:legal@x.es", "tel:+34600"])
    def test_non_navigable_links_do_not_count(self, href):
        html = f'<a href="{href}">Aviso legal</a><a href="{href}">Privacidad</a>'
        assert {"no_legal_notice", "no_privacy_policy"} <= _issues(html)

    def test_terms_do_not_match_across_two_separate_links(self):
        html = '<a href="/a">Aviso</a><a href="/b">Legal</a>'
        assert "no_legal_notice" in _issues(html)

    def test_footer_of_a_contact_subpage_counts(self):
        # A footer that only renders on inner pages is common, and the sub-page was
        # already fetched while looking for the email — this costs no extra request.
        home = "<html><body><h1>Inicio</h1></body></html>"
        assert "no_legal_notice" not in _issues(home, '<a href="/aviso-legal">Aviso legal</a>')

    def test_documents_resolve_without_a_declared_language(self):
        soups = [_parse('<a href="/p/1">Privacy Policy</a>')]
        details = verify_documents(find_links(soups), BASE)
        assert details["privacy_policy"] == {
            "status": "ok", "found_url": f"{BASE}/p/1", "method": "link", "language": "en",
        }


# ── cookie banner ─────────────────────────────────────────────────────────────

class TestCookieBanner:
    @pytest.mark.parametrize("cmp_html", [
        '<script src="https://consent.cookiebot.com/uc.js"></script>',
        '<script src="/wp-content/plugins/complianz-gdpr/cookiebanner/js/complianz.min.js"></script>',
        '<div id="cmplz-cookiebanner-container"></div>',
        '<div class="cky-consent-container"></div>',
        '<script type="text/plain" data-cookieconsent="statistics"></script>',
        '<div id="segurseo-cookie-banner"></div>',
    ])
    def test_detects_common_cmps(self, cmp_html):
        assert "no_cookie_banner" not in _issues(TRACKER + FOOTER + cmp_html)

    def test_trackers_without_banner_is_an_issue(self):
        assert "no_cookie_banner" in _issues(TRACKER + FOOTER)

    def test_no_banner_and_no_trackers_is_not_an_issue(self):
        # A site with only technical cookies has no consent obligation. Reporting it
        # would tell a compliant business it is breaking the law.
        assert "no_cookie_banner" not in _issues(f"<html><body><h1>Despacho</h1>{FOOTER}</body></html>")

    def test_cookieless_analytics_does_not_require_a_banner(self):
        html = '<script src="https://plausible.io/js/script.js"></script>' + FOOTER
        assert "no_cookie_banner" not in _issues(html)

    def test_hand_rolled_banner_is_recognised(self):
        html = (TRACKER + FOOTER + '<div class="aviso-cookies-bar">Solo usamos cookies necesarias'
                '<button>Aceptar</button><button>Rechazar</button></div>')
        assert "no_cookie_banner" not in _issues(html)

    def test_unrelated_cookie_word_is_not_consent_ui(self):
        # A bakery's ".cookie-recipe" must not read as a banner: the generic matcher
        # needs a banner-ish word in the same token, which this lacks.
        html = TRACKER + FOOTER + '<div class="cookie-recipe">Receta de galletas</div>'
        assert "no_cookie_banner" in _issues(html)

    def test_complianz_type_text_plain_with_data_cmplz_attribute(self):
        html = TRACKER + FOOTER + '<script type="text/plain" data-cmplz-src="https://x.es/t.js"></script>'
        assert "no_cookie_banner" not in _issues(html)

    @pytest.mark.parametrize("youtube", [
        '<iframe src="https://www.youtube.com/embed/abc"></iframe>',
        '<iframe src="https://www.google.com/maps/embed?pb=1"></iframe>',
        '<script src="https://www.google.com/recaptcha/api.js"></script>',
    ])
    def test_embeds_that_set_cookies_require_a_banner(self, youtube):
        assert "no_cookie_banner" in _issues(youtube + FOOTER)

    def test_privacy_enhanced_youtube_embed_requires_nothing(self):
        html = '<iframe src="https://www.youtube-nocookie.com/embed/abc"></iframe>' + FOOTER
        assert "no_cookie_banner" not in _issues(html)

    def test_google_fonts_alone_does_not_demand_a_banner(self):
        # Serving fonts from Google's CDN is an international-transfer question, not
        # a cookie one. Treating it as a tracker would flag nearly every site.
        html = '<link href="https://fonts.googleapis.com/css?family=Lato" rel="stylesheet">' + FOOTER
        assert "no_cookie_banner" not in _issues(html)


# ── reject control (AEPD 2023) ────────────────────────────────────────────────

class TestRejectControl:
    def test_accept_only_banner_is_reported(self):
        html = TRACKER + FOOTER + '<div class="cookie-banner"><button>Aceptar todas</button></div>'
        assert "cookie_banner_without_reject" in _issues(html)

    def test_banner_with_a_reject_control_is_clean(self):
        html = (TRACKER + FOOTER + '<div class="cookie-banner"><button>Aceptar todas</button>'
                '<button>Rechazar</button></div>')
        assert "cookie_banner_without_reject" not in _issues(html)

    @pytest.mark.parametrize("banner", [
        '<div class="cookie-banner"><button>Alle akzeptieren</button><button>Ablehnen</button></div>',
        '<div class="cookie-banner"><button>Tout accepter</button><button>Refuser</button></div>',
        '<div class="cookie-banner"><button>Accept all</button><button>Only necessary</button></div>',
        '<div class="cookie-banner"><button>Hyväksy</button><button>Hylkää</button></div>',
    ])
    def test_reject_control_recognised_in_other_languages(self, banner):
        assert "cookie_banner_without_reject" not in _issues(TRACKER + FOOTER + banner)

    def test_reject_control_recognised_from_a_data_attribute(self):
        html = (TRACKER + FOOTER + '<div class="cookie-banner">'
                '<button data-cookie-action="accept-all">Sí</button>'
                '<button data-cookie-action="reject">No</button></div>')
        assert "cookie_banner_without_reject" not in _issues(html)

    def test_js_rendered_banner_reports_nothing(self):
        # Complianz ships an empty container and builds the buttons client-side. We
        # cannot see either control, so accusing the site of the most heavily
        # sanctioned cookie infringement would be a guess.
        html = TRACKER + FOOTER + '<div id="cmplz-cookiebanner-container"></div>'
        assert reject_status(_parse(html)) == "unknown"
        assert "cookie_banner_without_reject" not in _issues(html)

    def test_inline_cmp_configuration_is_read(self):
        html = (TRACKER + FOOTER +
                '<script>var cky_config={"acceptText":"Aceptar","rejectText":"Rechazar"};</script>')
        assert reject_status(_parse(html)) == "ok"


# ── cookie policy ─────────────────────────────────────────────────────────────

class TestCookiePolicy:
    def test_missing_policy_is_reported_when_trackers_are_present(self):
        assert "no_cookie_policy" in _issues(TRACKER)

    def test_policy_is_not_required_without_trackers(self):
        # Point of the gate: no non-exempt cookie, nothing to inform about.
        details = _documents("<html><body><h1>Inicio</h1></body></html>")["cookie_policy"]
        assert details == {"status": "not_applicable", "reason": "no_trackers"}
        assert "no_cookie_policy" not in _issues("<html><body><h1>Inicio</h1></body></html>")

    def test_detects_policy_by_link_text(self):
        assert "no_cookie_policy" not in _issues(TRACKER + '<a href="/legal">Política de cookies</a>')

    def test_detects_policy_by_slug(self):
        assert "no_cookie_policy" not in _issues(TRACKER + '<a href="/politica-cookies">Más info</a>')

    @pytest.mark.parametrize("href", ["#", "#aceptar", "javascript:void(0)", "mailto:info@x.es"])
    def test_banner_own_controls_do_not_count_as_a_policy(self, href):
        # A consent banner renders "Aceptar cookies" as an anchor; counting it would
        # report a policy page that does not exist.
        html = TRACKER + f'<div class="cookie-banner"><a href="{href}">Aceptar cookies</a></div>'
        assert "no_cookie_policy" in _issues(html)


# ── forms ─────────────────────────────────────────────────────────────────────

class TestFormConsent:
    def test_form_without_checkbox_or_privacy_link_is_reported(self):
        assert detect_form_consent(_parse(f"<form>{CONTACT_FIELDS}</form>")) == ["form_without_consent"]

    def test_consent_checkbox_clears_the_form(self):
        html = f'<form>{CONTACT_FIELDS}<input type="checkbox" name="acepto"></form>'
        assert detect_form_consent(_parse(html)) == []

    def test_privacy_link_without_a_checkbox_only_degrades_the_finding(self):
        # Informed is not consented (GDPR art. 7): the form still has no way for the
        # visitor to agree, so it is reported — just not as the harshest finding.
        html = f'<form>{CONTACT_FIELDS}<a href="/politica-privacidad">Privacidad</a></form>'
        assert detect_form_consent(_parse(html)) == ["form_consent_link_only"]

    def test_prechecked_consent_checkbox_is_the_worst_finding(self):
        # CJEU C-673/17 (Planet49): pre-ticked consent is no consent at all.
        html = (f'<form>{CONTACT_FIELDS}<label><input type="checkbox" checked>'
                'Acepto la política de privacidad</label></form>')
        assert detect_form_consent(_parse(html)) == ["form_consent_prechecked"]

    def test_prechecked_unrelated_checkbox_is_ignored(self):
        html = (f'<form>{CONTACT_FIELDS}<label><input type="checkbox" checked name="copia">'
                'Enviarme una copia</label></form>')
        assert detect_form_consent(_parse(html)) == []

    def test_privacy_link_in_another_language_is_recognised(self):
        html = f'<form>{CONTACT_FIELDS}<a href="/p/9">Datenschutzerklärung</a></form>'
        assert detect_form_consent(_parse(html)) == ["form_consent_link_only"]

    def test_search_box_is_not_a_contact_form(self):
        html = '<form><input type="text" name="s"><input type="submit" value="Buscar"></form>'
        assert detect_form_consent(_parse(html)) == []

    def test_textarea_counts_towards_the_field_floor(self):
        html = '<form><input type="text" name="nombre"><textarea name="mensaje"></textarea></form>'
        assert detect_form_consent(_parse(html)) == ["form_without_consent"]

    def test_submit_and_hidden_inputs_do_not_count_as_fields(self):
        html = ('<form><input type="text" name="s"><input type="hidden" name="tok">'
                '<input type="submit"></form>')
        assert detect_form_consent(_parse(html)) == []

    def test_page_with_no_form_reports_nothing(self):
        assert detect_form_consent(_parse("<html><body><h1>Inicio</h1></body></html>")) == []

    def test_offending_form_on_a_contact_subpage_is_found(self):
        home = "<html><body><h1>Inicio</h1></body></html>"
        assert "form_without_consent" in _issues(home, f"<form>{CONTACT_FIELDS}</form>")

    def test_findings_are_ordered_by_severity(self):
        html = (f'<form>{CONTACT_FIELDS}</form>'
                f'<form>{CONTACT_FIELDS}<label><input type="checkbox" checked>Acepto la privacidad'
                '</label></form>')
        assert detect_form_consent(_parse(html)) == ["form_consent_prechecked", "form_without_consent"]

    def test_no_soups_reports_nothing(self):
        assert detect_form_consent() == []


# ── whole-site behaviour ──────────────────────────────────────────────────────

# Real markup captured from a live SegurSEO theme install (WP 7.1), not a
# hand-typed approximation, so the fixture reflects what the theme actually
# renders rather than what we assume it renders.
SEGURSEO_THEME_BANNER = """<div id="segurseo-cookie-consent" class="segurseo-cookie-consent segurseo-cookie-banner" hidden>
	<div class="segurseo-cookie-consent__bar">
		<p class="segurseo-cookie-consent__text">
			Utilizamos cookies propias y de terceros en SegurSEO para analizar el uso de la web y mejorar tu experiencia. Puedes aceptarlas, rechazarlas o configurar tus preferencias. <a href="http://localhost/politica-de-cookies/">Más información</a>		</p>
		<div class="segurseo-cookie-consent__actions">
			<button type="button" class="segurseo-cookie-consent__btn segurseo-cookie-consent__btn--ghost" data-cookie-action="configure">Configurar</button>
			<button type="button" class="segurseo-cookie-consent__btn segurseo-cookie-consent__btn--ghost" data-cookie-action="reject">Rechazar</button>
			<button type="button" class="segurseo-cookie-consent__btn segurseo-cookie-consent__btn--accept" data-cookie-action="accept-all">Aceptar todas</button>
		</div>
	</div>
</div>"""


class TestFullAudit:
    def test_no_false_positive_on_a_compliant_wordpress_site(self):
        html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Mi Empresa</title>
<link rel="stylesheet" href="/wp-content/plugins/complianz-gdpr/cookiebanner/css/cookiebanner.min.css">
{TRACKER}
</head>
<body>
<h1>Bienvenidos</h1>
{FOOTER}
<div id="cmplz-cookiebanner-container" class="cmplz-cookiebanner"></div>
<form>{CONTACT_FIELDS}<label><input type="checkbox" name="rgpd"> Acepto la política de privacidad</label></form>
</body>
</html>"""
        issues = _issues(html)
        assert issues == set(), f"False positive on a compliant site: {issues}"

    def test_segurseo_theme_marker_matches_real_output(self):
        # Asserts has_cmp directly rather than going through the audit: that path
        # also consults has_generic_banner, which matches this markup on its own and
        # would keep the assertion green with a broken _CMP_DOM_MARKERS entry.
        html = TRACKER + SEGURSEO_THEME_BANNER
        assert has_cmp(html.lower(), _parse(html))

    def test_real_segurseo_theme_output_is_reported_clean(self):
        # Our own client sites must never be reported as having no cookie banner, nor
        # as hiding the reject control, by whichever matcher gets there.
        html = TRACKER + FOOTER + SEGURSEO_THEME_BANNER
        assert has_generic_banner(_parse(html))
        assert _issues(html) == set()

    def test_details_accompany_every_document(self):
        details = _documents(TRACKER + FOOTER)
        assert set(details) == {"legal_notice", "privacy_policy", "cookie_policy"}
        assert details["privacy_policy"]["found_url"] == f"{BASE}/politica-de-privacidad"

    def test_issues_are_returned_as_key_to_spanish_label(self):
        issues = _audit(TRACKER)["compliance_issues"]
        assert "LSSI" in issues["no_legal_notice"]
        assert all(isinstance(v, str) and v for v in issues.values())


# ── verification (requests) ───────────────────────────────────────────────────

def _pad(text: str) -> str:
    """Pad a fixture past the minimum length of a real legal document."""
    return text + " " + "Texto adicional del documento publicado por la empresa. " * 8


LEGAL_NOTICE_PAGE = _pad(
    "Aviso legal. Titular: Ejemplo SL, CIF B65432197, con domicilio en Calle Mayor 3, "
    "03201 Elche (Alicante). Correo electrónico de contacto: info@ejemplo.es.")

PRIVACY_PAGE = _pad(
    "Política de privacidad. El responsable del tratamiento es Ejemplo SL. Finalidad: "
    "atender sus consultas. Base jurídica: el consentimiento del interesado. Derechos: "
    "acceso, rectificación, supresión, oposición y portabilidad. Conservación: los datos "
    "se conservarán durante el plazo legal. Puede presentar una reclamación ante la AEPD.")

COOKIE_PAGE = _pad(
    "Política de cookies. Utilizamos cookies técnicas, analíticas y publicitarias, propias "
    "y de terceros. Duración: las cookies de sesión caducan al cerrar el navegador y las "
    "persistentes tienen una vigencia de 24 meses.")

INCOMPLETE_LEGAL_NOTICE = _pad(
    "Aviso legal. Bienvenido a nuestra web. El uso de este sitio implica la aceptación de "
    "las presentes condiciones generales de uso.")


def _page(text: str) -> str:
    return f"<html><body><main>{text}</main></body></html>"


def _fetcher(pages: dict, calls: list | None = None):
    """Build a fetch callable over a URL → (status, html) map. Unknown URLs 404."""
    def fetch(url):
        if calls is not None:
            calls.append(url)
        return pages.get(url, (404, None))
    return fetch


class TestDocumentVerification:
    def test_linked_and_complete_document_is_ok(self):
        html = TRACKER + FOOTER + SEGURSEO_THEME_BANNER
        pages = {f"{BASE}/aviso-legal": (200, _page(LEGAL_NOTICE_PAGE)),
                 f"{BASE}/politica-de-privacidad": (200, _page(PRIVACY_PAGE)),
                 f"{BASE}/politica-de-cookies": (200, _page(COOKIE_PAGE))}
        result = _audit(html, fetch=_fetcher(pages))
        assert result["compliance_details"]["legal_notice"] == {
            "status": "ok", "found_url": f"{BASE}/aviso-legal", "method": "link",
            "language": "es", "checked_urls": [f"{BASE}/aviso-legal"], "validated": True,
        }
        assert result["compliance_issues"] == {}

    def test_broken_link_is_reported_as_broken_not_missing(self):
        # The distinction matters on the call: "you link it and it 404s" is a very
        # different conversation from "you never published it".
        detail = _audit(FOOTER, fetch=_fetcher({}))["compliance_details"]["legal_notice"]
        assert detail["status"] == "broken_link"
        assert detail["http_status"] == 404
        assert "legal_notice_broken" in _audit(FOOTER, fetch=_fetcher({}))["compliance_issues"]

    def test_unlinked_page_found_by_probing(self):
        pages = {f"{BASE}/aviso-legal": (200, _page(LEGAL_NOTICE_PAGE))}
        result = _audit("<html><body><h1>Inicio</h1></body></html>", fetch=_fetcher(pages))
        detail = result["compliance_details"]["legal_notice"]
        assert detail["status"] == "unlinked"
        assert detail["method"] == "probe"
        assert detail["found_url"] == f"{BASE}/aviso-legal"
        assert "legal_notice_unlinked" in result["compliance_issues"]

    def test_nothing_anywhere_is_missing(self):
        result = _audit("<html><body><h1>Inicio</h1></body></html>", fetch=_fetcher({}))
        detail = result["compliance_details"]["legal_notice"]
        assert detail["status"] == "missing"
        assert len(detail["checked_urls"]) >= 4
        assert "no_legal_notice" in result["compliance_issues"]

    def test_empty_template_page_does_not_count_as_published(self):
        # WordPress themes ship an /aviso-legal with a heading and nothing else.
        pages = {f"{BASE}/aviso-legal": (200, _page("Aviso legal"))}
        detail = _audit(FOOTER, fetch=_fetcher(pages))["compliance_details"]["legal_notice"]
        assert detail["status"] == "missing"
        assert detail["reason"] == "empty_page"

    def test_incomplete_legal_notice_names_what_is_missing(self):
        pages = {f"{BASE}/aviso-legal": (200, _page(INCOMPLETE_LEGAL_NOTICE))}
        result = _audit(FOOTER, fetch=_fetcher(pages))
        detail = result["compliance_details"]["legal_notice"]
        assert detail["status"] == "incomplete"
        assert detail["missing_items"] == ["tax_id", "address", "contact"]
        assert "NIF/CIF" in result["compliance_issues"]["legal_notice_incomplete"]

    def test_incomplete_privacy_policy_names_the_missing_article_13_points(self):
        pages = {f"{BASE}/politica-de-privacidad":
                 (200, _page(_pad("Política de privacidad. Sus datos se tratan con la máxima "
                                  "confidencialidad por parte del responsable del tratamiento.")))}
        detail = _audit(FOOTER, fetch=_fetcher(pages))["compliance_details"]["privacy_policy"]
        assert detail["status"] == "incomplete"
        assert {"purpose", "legal_basis", "retention", "authority"} <= set(detail["missing_items"])

    def test_regulated_profession_must_state_its_bar_association(self):
        pages = {f"{BASE}/aviso-legal": (200, _page(LEGAL_NOTICE_PAGE))}
        detail = _audit(FOOTER, fetch=_fetcher(pages), profession="abogados"
                        )["compliance_details"]["legal_notice"]
        assert detail["missing_items"] == ["bar_association"]

    def test_unregulated_profession_is_not_asked_for_one(self):
        pages = {f"{BASE}/aviso-legal": (200, _page(LEGAL_NOTICE_PAGE))}
        detail = _audit(FOOTER, fetch=_fetcher(pages), profession="peluquerías"
                        )["compliance_details"]["legal_notice"]
        assert detail["status"] == "ok"

    def test_pdf_legal_notice_is_published_but_unvalidated(self):
        # The fetcher reports a body it cannot read; the document is still published.
        detail = _audit(FOOTER, fetch=_fetcher({f"{BASE}/aviso-legal": (200, None)})
                        )["compliance_details"]["legal_notice"]
        assert detail["status"] == "ok"
        assert detail["validated"] is False

    def test_server_that_stops_answering_is_unknown_not_missing(self):
        # Not knowing is not evidence of breaching: no issue is reported.
        result = _audit("<html><body><h1>Inicio</h1></body></html>", fetch=lambda url: None)
        assert result["compliance_details"]["legal_notice"]["status"] == "unknown"
        assert result["compliance_issues"] == {}

    def test_request_budget_is_never_exceeded(self):
        calls = []
        _audit(TRACKER, fetch=_fetcher({}, calls))
        assert len(calls) <= MAX_COMPLIANCE_REQUESTS

    def test_budget_is_shared_so_the_last_document_is_still_checked(self):
        # Without a fair share the first document spends everything and the cookie
        # policy — a prime finding — would always come back "unknown".
        calls = []
        _audit(TRACKER, fetch=_fetcher({}, calls))
        assert any("cookie" in url for url in calls)

    def test_cookie_policy_is_not_requested_without_trackers(self):
        calls = []
        _audit("<html><body><h1>Inicio</h1></body></html>", fetch=_fetcher({}, calls))
        assert not any("cookie" in url for url in calls)

    def test_static_audit_is_unchanged_when_no_fetch_is_given(self):
        detail = _audit(FOOTER)["compliance_details"]["legal_notice"]
        assert detail == {"status": "ok", "found_url": f"{BASE}/aviso-legal",
                          "method": "link", "language": "es"}


# ── browser rendering fallback ────────────────────────────────────────────────

class TestRenderFallback:
    JS_FOOTER = f'<html lang="es"><body><h1>Inicio</h1>{FOOTER}</body></html>'

    def test_rendered_dom_supplies_the_links_the_static_html_hid(self):
        result = _audit("<html><body><div id='app'></div></body></html>",
                        render=lambda url: self.JS_FOOTER)
        assert result["compliance_issues"] == {}
        assert result["compliance_details"]["legal_notice"]["status"] == "ok"

    def test_render_is_not_paid_for_when_a_link_was_already_found(self):
        calls = []
        _audit(FOOTER, render=lambda url: calls.append(url) or self.JS_FOOTER)
        assert calls == []

    def test_render_failure_falls_back_to_the_static_verdict(self):
        result = _audit("<html><body><h1>Inicio</h1></body></html>", render=lambda url: None)
        assert "no_legal_notice" in result["compliance_issues"]

    def test_static_banner_survives_a_render_that_lost_it(self):
        # A render that comes back stripped must never turn a published banner into
        # a finding: both DOMs are consulted, not just the newer one.
        html = (TRACKER + '<div class="cookie-banner"><button>Aceptar todas</button>'
                '<button>Rechazar</button></div>')
        issues = _audit(html, render=lambda url: "<html><body></body></html>")["compliance_issues"]
        assert "no_cookie_banner" not in issues
        assert "cookie_banner_without_reject" not in issues

    def test_banner_rendered_client_side_is_judged_on_the_rendered_dom(self):
        # The reason the fallback re-runs every check and not only the links: the
        # banner is exactly the part of the page that never arrives in the HTML.
        rendered = (f'<html lang="es"><body>{FOOTER}'
                    '<div class="cookie-banner"><button>Aceptar todas</button></div></body></html>')
        result = _audit(TRACKER, render=lambda url: rendered)
        assert "cookie_banner_without_reject" in result["compliance_issues"]
        assert "no_cookie_banner" not in result["compliance_issues"]


# ── untrusted hrefs (SSRF) ────────────────────────────────────────────────────

class TestUntrustedLinkTargets:
    """Hrefs come from the analyzed site, so every destination is untrusted input."""

    INTERNAL = ["http://169.254.169.254/latest/meta-data/",   # cloud metadata
                "http://127.0.0.1:8000/admin",
                "http://192.168.1.1/",
                "http://[::1]/"]

    @pytest.mark.parametrize("href", INTERNAL)
    def test_a_legal_link_to_an_internal_address_is_never_followed(self, href):
        calls = []
        _audit(f'<a href="{href}">Aviso legal</a>', fetch=_fetcher({}, calls))
        assert not any(url.startswith(href[:20]) for url in calls)

    @pytest.mark.parametrize("href", INTERNAL)
    def test_an_internal_address_never_reaches_the_panel(self, href):
        # found_url is rendered as a link for the agent to click: an address we
        # refused to fetch must not be handed to their browser instead.
        detail = _audit(f'<a href="{href}">Aviso legal</a>',
                        fetch=_fetcher({}))["compliance_details"]["legal_notice"]
        assert detail["found_url"] is None
        assert not any(href in url for url in detail.get("checked_urls", []))

    def test_the_document_is_still_probed_when_its_link_is_rejected(self):
        # Dropping the link must not hand the site a way to skip the audit: the
        # document is probed exactly as if it had never been linked.
        pages = {f"{BASE}/aviso-legal": (200, _page(LEGAL_NOTICE_PAGE))}
        detail = _audit('<a href="http://127.0.0.1/admin">Aviso legal</a>',
                        fetch=_fetcher(pages))["compliance_details"]["legal_notice"]
        assert detail["status"] == "unlinked"
        assert detail["found_url"] == f"{BASE}/aviso-legal"

    def test_a_legal_page_on_another_public_domain_is_still_followed(self):
        # policies.python.org is the real case: the guard must reject internal
        # hosts without rejecting legitimate cross-domain policies.
        url = "https://policies.example.org/privacy"
        detail = _audit(f'<a href="{url}">Privacy Policy</a>',
                        fetch=_fetcher({url: (200, _page(PRIVACY_PAGE))})
                        )["compliance_details"]["privacy_policy"]
        assert detail["status"] == "ok"
        assert detail["found_url"] == url


class TestProbeBudgetEfficiency:
    def test_probing_stops_once_the_server_stops_answering(self):
        # Six paths per document at a full connect timeout each is the audit's
        # worst case; a host that is down should not be charged for all of them.
        calls = []
        result = _audit("<html><body><h1>Inicio</h1></body></html>",
                        fetch=lambda url: calls.append(url) or None)
        assert len(calls) <= 4, f"kept probing a dead host: {calls}"
        assert result["compliance_details"]["legal_notice"]["status"] == "unknown"

    def test_an_isolated_failure_does_not_end_the_probe(self):
        # A single flaky path is not a dead server: the document is still found.
        pages = {f"{BASE}/aviso-legal-y-politica-de-privacidad": (200, _page(LEGAL_NOTICE_PAGE))}

        def fetch(url):
            return None if url.endswith("/legal") else pages.get(url, (404, None))

        detail = _audit("<html><body><h1>Inicio</h1></body></html>",
                        fetch=fetch)["compliance_details"]["legal_notice"]
        assert detail["status"] == "unlinked"
