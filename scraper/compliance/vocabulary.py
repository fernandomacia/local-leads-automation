"""Multilingual lexicon for legal-document detection. Data only, no logic.

Two independent axes, because they diverge in practice:

* **Terms** — what a link says to a human, grouped by language so a match can
  report which language it fired on.
* **Slugs** — what the URL says. Multilingual sites routinely keep an English
  or Spanish slug behind a translated label, and vice versa.

Terms are written unaccented and lowercase: :func:`~scraper.compliance.matching.normalize`
folds the page text the same way before matching, so ``Política`` reaches the
matcher as ``politica``. Greek and Cyrillic are written in their own alphabet.

Coverage is EU-wide plus Russian, not because the pipeline targets those
markets, but because a Spanish business with a German or English site is
otherwise reported as having no legal notice at all — the single most damaging
false positive this analyser can produce, since it is said out loud on a sales
call.
"""

# ── Legal notice (LSSI art. 10 / Impressum / mentions légales) ────────────────

LEGAL_NOTICE_TERMS: dict[str, tuple[str, ...]] = {
    "es": ("aviso legal", "informacion legal", "nota legal", "datos identificativos"),
    "ca": ("avis legal", "informacio legal"),
    "gl": ("aviso legal",),
    "eu": ("lege oharra",),
    "en": ("legal notice", "legal information", "imprint", "company information"),
    "de": ("impressum", "rechtliche hinweise", "anbieterkennzeichnung"),
    "fr": ("mentions legales", "informations legales"),
    "it": ("note legali", "informazioni legali", "dati societari"),
    "pt": ("aviso legal", "informacao legal"),
    "nl": ("juridische kennisgeving", "colofon", "wettelijke informatie"),
    "pl": ("nota prawna", "informacje prawne"),
    "ro": ("mentiuni legale", "informatii legale"),
    "sv": ("juridisk information",),
    "da": ("juridisk meddelelse",),
    "fi": ("oikeudellinen huomautus",),
    "el": ("νομικες πληροφοριες", "νομικη σημειωση"),
    "cs": ("pravni informace",),
    "sk": ("pravne informacie",),
    "hu": ("jogi nyilatkozat",),
    "bg": ("правна информация",),
    "hr": ("pravne informacije",),
    "sl": ("pravno obvestilo",),
    "lt": ("teisine informacija",),
    "lv": ("juridiska informacija",),
    "et": ("oiguslik teave",),
    "ga": ("fogra dlithiuil",),
    "mt": ("avviz legali",),
    "ru": ("правовая информация", "правовые уведомления", "реквизиты"),
}

# ── Privacy policy (GDPR art. 13) ────────────────────────────────────────────

PRIVACY_TERMS: dict[str, tuple[str, ...]] = {
    "es": ("politica de privacidad", "proteccion de datos", "privacidad"),
    "ca": ("politica de privacitat", "proteccio de dades", "privacitat"),
    "gl": ("politica de privacidade",),
    "eu": ("pribatutasun politika",),
    "en": ("privacy policy", "privacy notice", "privacy statement", "data protection"),
    "de": ("datenschutz", "datenschutzerklarung", "datenschutzhinweise"),
    "fr": ("politique de confidentialite", "protection des donnees", "vie privee"),
    "it": ("informativa sulla privacy", "privacy policy", "protezione dei dati"),
    "pt": ("politica de privacidade", "protecao de dados"),
    "nl": ("privacybeleid", "privacyverklaring", "gegevensbescherming"),
    "pl": ("polityka prywatnosci", "ochrona danych"),
    "ro": ("politica de confidentialitate", "protectia datelor"),
    "sv": ("integritetspolicy",),
    "da": ("privatlivspolitik",),
    "fi": ("tietosuojaseloste", "tietosuojakaytanto"),
    "el": ("πολιτικη απορρητου",),
    "cs": ("ochrana osobnich udaju",),
    "sk": ("ochrana osobnych udajov",),
    "hu": ("adatvedelmi tajekoztato",),
    "bg": ("политика за поверителност",),
    "hr": ("politika privatnosti",),
    "sl": ("politika zasebnosti",),
    "lt": ("privatumo politika",),
    "lv": ("privatuma politika",),
    "et": ("privaatsuspoliitika",),
    "ru": ("политика конфиденциальности", "обработка персональных данных", "защита данных"),
}

# ── Cookie policy ────────────────────────────────────────────────────────────

# Half of these languages do not contain the substring "cookie" at all — fi, sv,
# hr, sl, hu, bg, lv, et. Any filter built on that substring reports every site
# in those languages as having no cookie policy.
COOKIE_TERMS: dict[str, tuple[str, ...]] = {
    "es": ("politica de cookies", "uso de cookies", "cookies"),
    "ca": ("politica de galetes", "galetes"),
    "en": ("cookie policy", "cookie notice", "cookies"),
    "de": ("cookie-richtlinie", "cookie-hinweise"),
    "fr": ("politique de cookies", "politique relative aux cookies"),
    "it": ("informativa sui cookie", "cookie policy"),
    "pt": ("politica de cookies",),
    "nl": ("cookiebeleid", "cookieverklaring"),
    "pl": ("polityka cookies", "polityka plikow cookies"),
    "ro": ("politica de cookie-uri",),
    "fi": ("evasteet", "evastekaytanto"),
    "sv": ("kakor", "cookiepolicy"),
    "da": ("cookiepolitik",),
    "hr": ("kolacici",),
    "sl": ("piskotki",),
    "hu": ("sutik", "suti szabalyzat"),
    "bg": ("бисквитки",),
    "lv": ("sikdatnes",),
    "et": ("kupsised",),
    "lt": ("slapukai",),
    "el": ("πολιτικη cookies",),
    "ru": ("файлы cookie", "куки", "политика использования файлов cookie"),
}

# ── URL slugs ────────────────────────────────────────────────────────────────

# Matched against individual path segments, never against the whole URL: the
# old substring test reported "/asesoria-legal" (a law firm's services page) as
# the legal notice.

LEGAL_NOTICE_SLUGS: tuple[str, ...] = (
    "aviso-legal", "avis-legal", "nota-legal", "legal-notice", "legal-info",
    "legal-information", "impressum", "imprint", "mentions-legales",
    "note-legali", "nota-prawna", "mentiuni-legale", "colofon", "juridisch",
    "pravna-informacija", "pravovaya-informatsiya", "lege-oharra",
)

PRIVACY_SLUGS: tuple[str, ...] = (
    "politica-de-privacidad", "politica-privacidad", "privacidad", "privacitat",
    "privacy-policy", "privacy", "datenschutz", "confidentialite",
    "privacidade", "privacybeleid", "polityka-prywatnosci",
    "confidentialitate", "integritetspolicy", "tietosuoja",
    "konfidencialnosti", "proteccion-datos", "proteccion-de-datos", "gdpr", "rgpd",
)

COOKIE_SLUGS: tuple[str, ...] = (
    "politica-de-cookies", "politica-cookies", "cookie-policy", "cookies",
    "cookie-richtlinie", "cookiebeleid", "evasteet", "kakor", "kolacici",
    "piskotki", "sutik", "cookie-uri", "galetes", "cookiepolitik",
)

# Keyed by the document ids used throughout the package and in compliance_details.
DOCUMENT_TERMS: dict[str, dict[str, tuple[str, ...]]] = {
    "legal_notice": LEGAL_NOTICE_TERMS,
    "privacy_policy": PRIVACY_TERMS,
    "cookie_policy": COOKIE_TERMS,
}

DOCUMENT_SLUGS: dict[str, tuple[str, ...]] = {
    "legal_notice": LEGAL_NOTICE_SLUGS,
    "privacy_policy": PRIVACY_SLUGS,
    "cookie_policy": COOKIE_SLUGS,
}

# ── Consent banner controls ──────────────────────────────────────────────────

# Rejecting must be as easy as accepting (AEPD cookie guide, 2023; EDPB
# cookie-banner taskforce report). Detecting the *absence* of a reject control
# is only meaningful when an accept control was visible in the same markup,
# hence both lists.
ACCEPT_TERMS: tuple[str, ...] = (
    "aceptar", "acepto", "accepta", "accept", "agree", "allow", "got it",
    "entendido", "de acuerdo", "akzeptieren", "zustimmen", "einverstanden",
    "accepter", "j'accepte", "accetta", "accetto", "aceitar", "accepteren",
    "akkoord", "akceptuj", "zgadzam sie", "de acord",
    "godkann", "acceptera", "accepter alle", "hyvaksy", "prihvati", "sprejmi",
    "elfogad", "souhlasim", "prijimam", "suhlasim", "sutinku", "piekritu",
    "noustun", "приемам", "принять", "согласен", "αποδοχη", "συμφωνω",
)

REJECT_TERMS: tuple[str, ...] = (
    "rechazar", "rechazo", "rebutjar", "solo las necesarias", "solo esenciales",
    "continuar sin aceptar", "reject", "decline", "refuse", "deny",
    "only necessary", "necessary only", "essential only", "continue without accepting",
    "ablehnen", "alle ablehnen", "nur notwendige", "refuser", "tout refuser",
    "continuer sans accepter", "rifiuta", "rifiuto", "recusar", "weigeren",
    "afwijzen", "odrzuc", "odrzucam", "respinge", "refuza", "avvisa", "neka",
    "afvis", "hylkaa", "odbij", "zavrni", "elutasit", "odmitnout", "nesouhlasim",
    "odmietnut", "atmesti", "noraidit", "keeldu", "отклонить", "отказ",
    "отхвърляне", "απορριψη", "αρνηση",
)

# A hand-rolled banner is identified by a cookie word and a banner word sharing
# one id or class, so an unrelated ".cookie-recipe" is never mistaken for consent UI.
BANNER_WORDS: tuple[str, ...] = ("cookie", "galleta", "galeta", "consent", "consentimiento")
BANNER_CONTEXT: tuple[str, ...] = (
    "banner", "consent", "notice", "aviso", "bar", "popup", "modal",
    "gdpr", "rgpd", "lopd", "overlay", "dialog",
)

# ── Form consent ─────────────────────────────────────────────────────────────

# Words that identify a checkbox as the consent checkbox rather than a newsletter
# opt-in or a "remember me". Only a consent checkbox is worth reporting as
# pre-ticked; anything else is noise on a sales call.
CONSENT_TERMS: tuple[str, ...] = (
    "acepto", "accepto", "consiento", "autorizo", "he leido", "consentimiento",
    "consent", "i agree", "i accept", "i have read", "agree", "akzeptiere",
    "einverstanden", "zustimmung", "j'accepte", "acconsento", "aceito",
    "ga akkoord", "zgadzam sie", "sunt de acord", "godkanner", "hyvaksyn",
    "soglasen", "соглашаюсь", "rgpd", "gdpr", "lopd",
)

# ── Probe paths ──────────────────────────────────────────────────────────────

# Tried, in order, when no link to the document was found. A page that answers
# here exists but is not linked, which is its own infringement: the LSSI and the
# GDPR require the texts to be directly and permanently accessible. Kept short on
# purpose — every entry is a request, and the budget is per lead.
PROBE_PATHS: dict[str, tuple[str, ...]] = {
    "legal_notice": ("/aviso-legal", "/legal", "/aviso-legal-y-politica-de-privacidad",
                     "/impressum", "/legal-notice", "/avis-legal"),
    "privacy_policy": ("/politica-de-privacidad", "/privacidad", "/politica-privacidad",
                       "/privacy-policy", "/privacy", "/datenschutz"),
    "cookie_policy": ("/politica-de-cookies", "/politica-cookies", "/cookies",
                      "/aviso-de-cookies", "/cookie-policy", "/cookiebeleid"),
}

# ── Mandatory content ────────────────────────────────────────────────────────

# What each document must actually say. Terms are pooled across languages rather
# than grouped: here we only need to know whether the point is covered, not which
# language covers it.
CONTENT_REQUIREMENTS: dict[str, dict[str, tuple[str, ...]]] = {
    # LSSI art. 10: identity, tax id, registered address, and a direct contact.
    "legal_notice": {
        "address": ("domicilio", "direccion", "sede social", "calle", "avenida", "plaza",
                    "carrer", "address", "registered office", "anschrift", "sitz", "adresse"),
        "contact": ("correo electronico", "email", "e-mail", "telefono", "contacto",
                    "contact", "telephone", "kontakt"),
    },
    # GDPR art. 13: the six points a data subject must be told before collection.
    "privacy_policy": {
        "controller": ("responsable del tratamiento", "responsable", "titular", "controller",
                       "data controller", "responsible", "who we are", "verantwortlicher",
                       "titulaire"),
        "purpose": ("finalidad", "finalidades", "para que", "purpose", "purposes",
                    "why we collect", "how we use", "zweck"),
        "legal_basis": ("base juridica", "base legal", "legitimacion", "legitimidad",
                        "interes legitimo", "consentimiento", "legal basis", "lawful basis",
                        "legitimate interest", "rechtsgrundlage"),
        "rights": ("derechos", "acceso", "rectificacion", "supresion", "portabilidad",
                   "oposicion", "rights", "erasure", "betroffenenrechte"),
        "retention": ("conservacion", "conservaran", "plazo", "durante cuanto tiempo",
                      "retention", "retain", "how long", "speicherdauer"),
        "authority": ("autoridad de control", "agencia espanola de proteccion de datos",
                      "aepd", "reclamacion", "supervisory authority", "data protection authority",
                      "complaint", "aufsichtsbehorde"),
    },
    # A cookie policy that lists no cookie is not a cookie policy.
    "cookie_policy": {
        "categories": ("tecnicas", "necesarias", "analiticas", "analisis", "publicitarias",
                       "preferencias", "estadisticas", "marketing", "terceros",
                       "necessary", "analytics", "advertising", "statistics"),
        "duration": ("duracion", "caducidad", "plazo", "vigencia", "sesion", "persistente",
                     "meses", "anos", "dias", "duration", "expiry", "session", "persistent",
                     "laufzeit"),
    },
}

# Spanish names for each requirement, interpolated into the issue label so the
# agent reads exactly what is missing instead of "incomplete".
REQUIREMENT_LABELS: dict[str, str] = {
    "tax_id": "NIF/CIF",
    "address": "domicilio",
    "contact": "datos de contacto",
    "bar_association": "colegio profesional y número de colegiado",
    "controller": "responsable del tratamiento",
    "purpose": "finalidad del tratamiento",
    "legal_basis": "base jurídica",
    "rights": "derechos del interesado",
    "retention": "plazo de conservación",
    "authority": "autoridad de control",
    "categories": "cookies o categorías instaladas",
    "duration": "duración de las cookies",
}

# Professions whose legal notice must also state the bar association and the
# membership number (LSSI art. 10.1.c). Matched against the search term the lead
# came from, not against the page.
REGULATED_PROFESSIONS: tuple[str, ...] = (
    "abogado", "abogada", "abogados", "procurador", "notaria", "notario",
    "arquitecto", "arquitecta", "aparejador", "ingeniero", "ingeniera",
    "medico", "medica", "clinica", "dentista", "odontologo", "fisioterapeuta",
    "psicologo", "psicologa", "veterinario", "veterinaria", "farmacia",
    "farmaceutico", "optico", "optica", "podologo", "nutricionista",
    "economista", "auditor", "graduado social", "administrador de fincas",
    "agente de la propiedad inmobiliaria", "api", "gestoria", "asesoria juridica",
)

# Languages whose sites are subject to the Spanish LSSI, where a missing tax id
# is a finding. A German or French site has its own equivalent, checked through
# the generic identity requirements instead.
SPANISH_LANGUAGES: frozenset[str] = frozenset({"es", "ca", "gl", "eu"})
