"""Personalized outreach email and phone script generation via the OpenRouter API.

The model receives raw structured data about a lead's web presence and returns
a JSON object with a complete email (subject + body) and a phone argumentario.
"""

import json
import re

import requests

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL, SENDER_COMPANY, SOCIAL_DOMAINS

if not SENDER_COMPANY:
    raise EnvironmentError("SENDER_COMPANY must be set in .env")

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = f"""Eres un agente comercial de {SENDER_COMPANY}, empresa especializada en diseño web y SEO para negocios locales.
Tu tarea es generar materiales de venta personalizados para un negocio local.

Recibirás datos sobre el negocio. Transforma la información en argumentos de negocio concretos: pérdida de visibilidad, falta de credibilidad o clientes potenciales que no llegan. Nunca menciones tecnicismos directamente, solo sus consecuencias reales.

ESCENARIO A — El negocio tiene sitio web (tiene_web: true):
Genera email completo Y argumentario. El pitch se basa en los problemas SEO detectados en la web y los de la ficha de Google Maps.
- Caso especial: si cms es "unreachable", el sitio web existe en la ficha de Google Maps pero está caído o no es accesible. El pitch principal es que los clientes que buscan el negocio en Google no pueden acceder a la web (pérdida directa de clientes). No menciones análisis SEO técnico. Usa este problema como argumento para ofrecer un sitio nuevo o la recuperación del actual.

ESCENARIO B — El negocio NO tiene sitio web (tiene_web: false):
El pitch principal es la ausencia de presencia online y los problemas de la ficha de Google Maps.
- Si hay email_contacto: genera email completo Y argumentario.
- Si NO hay email_contacto: devuelve subject y body como cadenas vacías; genera solo argumentario.

NORMAS DEL EMAIL:
- Tratamiento de usted en todo momento
- Estructura completa: saludo personalizado al equipo del negocio, presentación de {SENDER_COMPANY} como empresa especializada en diseño web y SEO, descripción de los problemas detectados y su impacto en el negocio, propuesta de auditoría gratuita y sin compromiso, despedida cordial, firma con el texto literal [NOMBRE] (sin modificarlo) seguido del nombre de la empresa {SENDER_COMPANY}
- El emisor es un representante de la empresa, no un especialista técnico personal
- Tono profesional pero cercano; directo, sin rodeos ni frases hechas
- Sin emojis, sin exclamaciones
- Máximo 250 palabras

NORMAS DEL ARGUMENTARIO:
- Estructurado en fases claramente etiquetadas: Apertura, Identificación del problema, Impacto en el negocio, Propuesta, Objeciones
- Frases cortas y directas, listas para leer en voz alta
- Al menos 2 objeciones frecuentes con su respuesta concisa
- Sin emojis, sin exclamaciones

Responde ÚNICAMENTE con JSON válido, sin texto adicional:
{{"subject": "asunto del email (máx. 60 caracteres, sin signos de exclamación)", "body": "email completo listo para enviar", "phone_script": "argumentario estructurado para la llamada"}}"""


def _build_prompt(lead: dict) -> str:
    social = {k: lead[k] for k in SOCIAL_DOMAINS if lead.get(k)}
    seo_issues = lead.get("seo_issues") or {}
    seo_problems = list(seo_issues.values()) if isinstance(seo_issues, dict) else []
    maps_issues = lead.get("maps_issues") or {}
    maps_problems = list(maps_issues.values()) if isinstance(maps_issues, dict) else []
    has_website = lead.get("has_website", bool(lead.get("website")))

    data = {
        "negocio": lead.get("lead", ""),
        "ciudad": lead.get("city", "") or "",
        "profesion": lead.get("profession", "") or "",
        "tiene_web": has_website,
        "web": lead.get("website", "") or "",
        "cms": lead.get("cms", "") or "",
        "puntuacion_seo": lead.get("seo_score"),
        "problemas_seo": seo_problems,
        "problemas_google_maps": maps_problems,
        "email_contacto": lead.get("email", "") or "",
        "redes_sociales": social,
    }

    return json.dumps(data, ensure_ascii=False, indent=2)


def _escape_string_newlines(s: str) -> str:
    """Replace literal newlines inside JSON string values with \\n.

    LLMs occasionally output raw newlines inside quoted strings, producing invalid JSON.
    Structural whitespace between fields is left untouched.
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


def _complete(user_prompt: str) -> str:
    """Send a chat completion request to OpenRouter and return the raw text."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 2000,
        "temperature": 0.6,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(_OPENROUTER_URL, headers=headers, json=payload, timeout=(5, 90))
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    # Strip markdown code fences — some models add them despite json_object mode
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw.rstrip()).strip()
    return raw


def _try_load_json(s: str) -> dict | None:
    """Parse s as JSON, retrying after escaping literal newlines. Returns None on failure."""
    for candidate in (s, _escape_string_newlines(s)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    return None


def _dict_to_text(d: dict) -> str:
    """Flatten a structured dict (phase label → content) into readable plain text."""
    lines = []
    for section, content in d.items():
        lines.append(f"--- {section} ---")
        if isinstance(content, dict):
            for k, v in content.items():
                lines.append(f"· {k}: {v}")
        else:
            lines.append(str(content))
    return "\n\n".join(lines)


def _parse(raw: str) -> dict:
    """Extract the JSON object from raw LLM output.

    Uses the full first-to-last brace span as the primary path, with a
    newline-escape retry for models that emit literal newlines inside strings.
    """
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError("No JSON object found in model output")
    result = _try_load_json(raw[start:end])
    if result is not None:
        return result
    raise ValueError("No valid JSON found")


def generate(lead: dict) -> dict:
    """Generate a personalized outreach email and phone argumentario for a lead.

    Args:
        lead: Enriched lead dict from web_analyzer, optionally extended with
              ``city`` and ``profession`` from the job payload.

    Returns:
        Dict with keys ``subject``, ``body``, and ``phone_script``.
        Falls back to stub values if the model output cannot be parsed.
    """
    name = lead.get("lead", "")

    raw = _complete(_build_prompt(lead))

    try:
        parsed = _parse(raw)

        phone_raw = parsed.get("phone_script") or ""
        phone_text = _dict_to_text(phone_raw) if isinstance(phone_raw, dict) else str(phone_raw)

        has_email_channel = lead.get("has_website") or bool(lead.get("email"))
        result = {
            "subject": (parsed.get("subject") or (f"Propuesta de mejora web para {name}" if has_email_channel else "")).strip(),
            "body": (parsed.get("body") or "").replace("\\n", "\n").strip(),
            "phone_script": phone_text.replace("\\n", "\n").strip(),
        }
        print(f"[AI] Generated for '{name}': {len(result['body'])}b email, {len(result['phone_script'])}b script")
        return result
    except (ValueError, KeyError) as exc:
        print(f"[!] AI parse failed for '{name}' ({exc}): {raw[:200]!r}")
        return {
            "subject": f"Propuesta de mejora web para {name}",
            "body": "",
            "phone_script": "",
        }
