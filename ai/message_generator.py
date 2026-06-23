"""Personalized outreach email generation via the OpenRouter API.

The model receives a structured prompt describing a lead's web presence and
returns a JSON object with a subject line and a central paragraph. The final
email is assembled around that paragraph using fixed intro and closing blocks.
"""

import json
import re

import requests

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL, SENDER_COMPANY, SENDER_NAME, SOCIAL_DOMAINS

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Each group maps a set of technical SEO issue keys to a single business-language
# label. The model only ever sees the label — never the raw issue name.
_ISSUE_GROUPS: dict[str, dict] = {
    "seguridad": {
        "issues": {"no_https"},
        "label": "la web presenta vulnerabilidades de seguridad que pueden generar desconfianza en sus visitantes",
    },
    "posicionamiento": {
        "issues": {"no_title", "no_meta_description", "no_h1", "multiple_h1",
                   "no_canonical", "no_structured_data", "no_sitemap", "no_robots"},
        "label": "el posicionamiento en Google está limitado por carencias técnicas no visibles a simple vista",
    },
    "adaptacion_movil": {
        "issues": {"no_viewport", "no_alt_images", "no_lang"},
        "label": "la web no está completamente adaptada para móviles ni cumple criterios de accesibilidad",
    },
    "redes_sociales": {
        "issues": {"no_og_tags"},
        "label": "el contenido no está preparado para compartirse correctamente en redes sociales",
    },
    "analitica": {
        "issues": {"no_analytics"},
        "label": "no dispone de herramientas para medir las visitas ni el comportamiento de sus clientes",
    },
    "imagen": {
        "issues": {"no_favicon"},
        "label": "hay detalles de imagen de marca que restan profesionalidad a la presencia online",
    },
}

SYSTEM_PROMPT = f"""Eres {SENDER_NAME}, especialista en diseño web y SEO en {SENDER_COMPANY}.
Redactas el párrafo central de emails a negocios locales describiendo los problemas detectados en su presencia online.

Normas estrictas:
- SIEMPRE tratamiento de usted: "le", "su", "usted", "les"
- Escribe SOLO un párrafo de 2-4 frases describiendo los problemas y su impacto en el negocio
- Usa lenguaje de negocio; NUNCA menciones tecnicismos, soluciones ni recomendaciones — solo describe las consecuencias
- No inventes problemas que no se te hayan indicado
- El campo "body" NUNCA puede estar vacío; escribe siempre al menos 2 frases aunque solo haya un problema
- Sin emojis, sin exclamaciones, sin frases hechas vacías

Responde ÚNICAMENTE con JSON válido, sin texto adicional:
{{"subject": "asunto del email (máx. 60 caracteres, sin signos de exclamación)", "body": "párrafo central con 2-4 frases"}}"""

_INTRO = (
    f"Me pongo en contacto con usted desde {SENDER_COMPANY}, "
    f"especialistas en diseño web y posicionamiento SEO para negocios locales. "
    f"Tras analizar la presencia online de su empresa, he identificado algunas áreas "
    f"que podrían estar limitando su visibilidad y credibilidad en Internet."
)

_AUDIT_OFFER = (
    f"Por ello, me gustaría ofrecerle una auditoría web y SEO completamente gratuita "
    f"y sin compromiso. Analizaremos su presencia online al detalle y le presentaremos "
    f"un informe personalizado con todas las oportunidades de mejora detectadas, "
    f"sin ninguna obligación por su parte."
)

_SIGNATURE = f"\n\nAtentamente,\n{SENDER_NAME}\n{SENDER_COMPANY}"

_SIGNATURE_PATTERNS = ("atentamente,", "saludos,", "un saludo,", "cordialmente,")

_FALLBACK_MIDDLE = (
    "Hemos detectado en su web algunas áreas de mejora que podrían estar "
    "afectando a su imagen y visibilidad ante sus clientes potenciales."
)


def _strip_signature(text: str) -> str:
    """Remove any LLM-generated signature block from the middle paragraph."""
    lines = text.rstrip().splitlines()
    while lines and any(lines[-1].lower().strip().startswith(p) for p in _SIGNATURE_PATTERNS):
        lines.pop()
    while lines and (SENDER_NAME in lines[-1] or SENDER_COMPANY in lines[-1]):
        lines.pop()
    return "\n".join(lines).strip()


def _active_groups(seo_issues: str) -> list[str]:
    """Return business-language labels for issue groups with at least one active issue."""
    active = set(seo_issues.split("|")) if seo_issues else set()
    return [data["label"] for data in _ISSUE_GROUPS.values() if active & data["issues"]]


def _build_prompt(lead: dict) -> str:
    name = lead.get("lead", "")
    city = lead.get("city", "").replace("*", "").strip()
    website = lead.get("website", "")
    cms = lead.get("cms", "")
    groups = _active_groups(lead.get("seo_issues", ""))
    is_social = any(d in website for d in SOCIAL_DOMAINS.values())

    lines = [f"Negocio: {name}"]
    if city:
        lines.append(f"Ciudad: {city}")

    if not website or is_social:
        lines.append("Situación: no dispone de página web propia (solo redes sociales o sin presencia online)")
        lines.append("Propuesta: diseño y desarrollo web desde cero")
    elif cms == "unreachable":
        lines.append(f"Web: {website}")
        lines.append("Situación: la web existe pero presenta errores de carga o acceso")
    elif cms == "wordpress":
        lines.append(f"Web: {website} (WordPress)")
        if groups:
            lines.append("Áreas con problemas detectados:")
            lines.extend(f"  - {g}" for g in groups)
        else:
            lines.append("La web no presenta problemas detectables, pero usa una plantilla genérica sin identidad de marca propia")
    elif cms in ("wix", "squarespace", "shopify"):
        lines.append(f"Web: {website} (plataforma {cms.capitalize()})")
        lines.append("Situación: las plataformas de este tipo limitan el SEO y la personalización frente a una web propia")
        if groups:
            lines.append("Problemas adicionales detectados:")
            lines.extend(f"  - {g}" for g in groups)
    else:
        lines.append(f"Web: {website}")
        if groups:
            lines.append("Áreas con problemas detectados:")
            lines.extend(f"  - {g}" for g in groups)
        else:
            lines.append("La web no presenta problemas detectables, aunque tiene margen de mejora en diseño y posicionamiento")

    return "\n".join(lines)


def _escape_string_newlines(s: str) -> str:
    """Replace literal newlines inside JSON string values with \\n.

    LLMs occasionally output raw newlines inside quoted strings, producing
    invalid JSON. Structural whitespace between fields is left untouched.
    """
    result = []
    in_string = False
    skip_next = False
    for ch in s:
        if skip_next:
            result.append(ch)
            skip_next = False
        elif ch == "\\" and in_string:
            result.append(ch)
            skip_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif ch == "\n" and in_string:
            result.append("\\n")
        else:
            result.append(ch)
    return "".join(result)


def _complete(system_prompt: str, user_prompt: str) -> str:
    """Send a chat completion request to OpenRouter and return the raw text."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 200,
        "temperature": 0.5,
    }
    resp = requests.post(_OPENROUTER_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _assemble(name: str, middle: str) -> str:
    """Compose the full email from its four fixed parts: greeting, intro, body, and offer."""
    return "\n\n".join([
        f"Estimado equipo de {name},",
        _INTRO,
        _strip_signature(middle),
        _AUDIT_OFFER,
    ]) + _SIGNATURE


def _try_load_json(s: str) -> dict | None:
    """Parse s as JSON, retrying after escaping literal newlines. Returns None on failure."""
    for candidate in (s, _escape_string_newlines(s)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    return None


def _parse(raw: str) -> dict:
    """Extract and merge all JSON objects from raw LLM output.

    First pass: collects every flat ``{...}`` block via regex and merges them,
    which handles models that split subject and body across separate objects.
    Full-span fallback handles a single well-formed object spanning multiple lines.
    """
    merged: dict = {}
    for m in re.finditer(r'\{[^{}]+\}', raw, re.DOTALL):
        result = _try_load_json(m.group())
        if result:
            merged.update(result)
    if merged:
        return merged

    start, end = raw.find("{"), raw.rfind("}") + 1
    result = _try_load_json(raw[start:end])
    if result:
        return result
    raise ValueError("No valid JSON found")


def generate(lead: dict) -> dict:
    """Generate a personalized outreach email for a lead.

    Args:
        lead: Enriched lead dict from web_analyzer.

    Returns:
        Dict with keys ``subject`` and ``body``. Falls back to a generic
        message if the model output cannot be parsed.
    """
    name = lead.get("lead", "")
    text = _complete(SYSTEM_PROMPT, _build_prompt(lead))

    try:
        parsed = _parse(text)
        middle = parsed.get("body", "")
        if isinstance(middle, dict):
            middle = middle.get("text", middle.get("content", str(middle)))
        middle = middle.replace("\\n", "\n").strip() or _FALLBACK_MIDDLE
        subject = parsed.get("subject", "").strip() or f"Propuesta de mejora web para {name}"
        return {"subject": subject, "body": _assemble(name, middle)}
    except (ValueError, KeyError):
        return {
            "subject": f"Propuesta de mejora web para {name}",
            "body": _assemble(name, _FALLBACK_MIDDLE),
        }
