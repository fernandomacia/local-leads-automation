"""Phone argumentario generation via the OpenRouter API.

The model receives raw structured data about a lead's web presence and returns
a JSON object with the argumentario the agent reads on the call. Outreach is
semi-manual by design, and a drafted email was one more thing to review before
sending; the call is where the pitch is actually made.
"""

import json
import logging
import random
import re
import time

import requests

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL, SENDER_COMPANY, SOCIAL_DOMAINS

logger = logging.getLogger(__name__)

if not SENDER_COMPANY:
    raise EnvironmentError("SENDER_COMPANY must be set in .env")

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


def _retry_delay(resp: requests.Response, attempt: int) -> float:
    """Return seconds to wait before the next attempt.

    Prefers the server's own hint (Retry-After or X-RateLimit-Reset);
    falls back to exponential backoff with jitter.
    """
    if retry_after := resp.headers.get("Retry-After"):
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    # OpenRouter sends X-RateLimit-Reset as epoch milliseconds
    if reset := resp.headers.get("X-RateLimit-Reset"):
        try:
            wait = float(reset) / 1000 - time.time()
            if 0 < wait <= 60:
                return wait
        except ValueError:
            pass
    return min(2 ** attempt + random.uniform(0, 1), 60.0)

SYSTEM_PROMPT = f"""Eres un agente comercial de {SENDER_COMPANY}, empresa especializada en diseño web y SEO para negocios locales.
Tu tarea es generar el argumentario de venta para la llamada a un negocio local.

Recibirás datos sobre el negocio. Transforma la información en argumentos de negocio concretos: pérdida de visibilidad, falta de credibilidad o clientes potenciales que no llegan. Nunca menciones tecnicismos directamente, solo sus consecuencias reales.

ESCENARIO A — El negocio tiene sitio web (tiene_web: true):
El pitch se basa en los problemas SEO detectados en la web y los de la ficha de Google Maps.
- Caso especial: si cms es "unreachable", el sitio web existe en la ficha de Google Maps pero está caído o no es accesible. El pitch principal es que los clientes que buscan el negocio en Google no pueden acceder a la web (pérdida directa de clientes). No menciones análisis SEO técnico. Usa este problema como argumento para ofrecer un sitio nuevo o la recuperación del actual.

ESCENARIO B — El negocio NO tiene sitio web (tiene_web: false):
El pitch principal es la ausencia de presencia online y los problemas de la ficha de Google Maps.

NORMAS DEL ARGUMENTARIO:
- Quien llama es un representante de {SENDER_COMPANY}, no un especialista técnico
- Tratamiento de usted en todo momento
- Tono profesional pero cercano; directo, sin rodeos ni frases hechas
- Estructurado en fases claramente etiquetadas: Apertura, Identificación del problema, Impacto en el negocio, Propuesta, Objeciones
- Frases cortas y directas, listas para leer en voz alta
- Al menos 2 objeciones frecuentes con su respuesta concisa
- Sin emojis, sin exclamaciones

CUMPLIMIENTO LEGAL:
Si recibes `problemas_legales`, trátalo como el argumento de MAYOR urgencia, por
encima de cualquier problema de SEO: es una obligación legal incumplida, no una
oportunidad de mejora.

Reglas estrictas al redactarlo:
- NUNCA menciones importes de multas, ni rangos, ni cifras de ningún tipo.
- NUNCA cites artículos, leyes concretas ni resoluciones.
- Habla del riesgo en términos generales: "es una obligación legal", "puede
  acarrear sanciones", "cualquier visitante puede denunciarlo".
- Mantén un tono informativo, nunca alarmista ni amenazante.

Orden de gravedad, de mayor a menor. No presentes los hallazgos como una lista
plana: abre por el más grave que aparezca y menciona el resto de forma breve y
agrupada, para no diluir el argumento principal.
1. Recoger datos personales sin base legal: formulario sin consentimiento o sin
   política de privacidad. Es lo más grave, porque ya hay datos de terceros
   tratados sin cobertura.
2. Falta de aviso legal: el negocio no está identificado como exige la normativa.
3. Rastreadores activos sin aviso de cookies: se está siguiendo al visitante
   antes de que pueda decidir.
4. Falta de política de cookies: incumplimiento informativo, el menos urgente.
Si solo hay hallazgos del nivel 4, no montes el argumentario entero sobre ellos: menciónalo
de pasada y apóyate en los problemas de SEO o de la ficha de Google Maps.

Responde ÚNICAMENTE con JSON válido, sin texto adicional:
{{"phone_script": "argumentario estructurado para la llamada"}}"""


def _build_prompt(lead: dict) -> str:
    social = {k: lead[k] for k in SOCIAL_DOMAINS if lead.get(k)}
    seo_issues = lead.get("seo_issues") or {}
    seo_problems = list(seo_issues.values()) if isinstance(seo_issues, dict) else []
    maps_issues = lead.get("maps_issues") or {}
    maps_problems = list(maps_issues.values()) if isinstance(maps_issues, dict) else []
    compliance_issues = lead.get("compliance_issues") or {}
    compliance_problems = list(compliance_issues.values()) if isinstance(compliance_issues, dict) else []
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
        "problemas_legales": compliance_problems,
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
    """Send a chat completion request to OpenRouter and return the raw text.

    Retries transient failures (rate limits, upstream 5xx) up to _MAX_ATTEMPTS
    times using server-hinted or exponential-backoff delays. 402 is never
    retried — it signals a billing issue handled upstream by the worker.
    """
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
    for attempt in range(_MAX_ATTEMPTS):
        resp = requests.post(_OPENROUTER_URL, headers=headers, json=payload, timeout=(5, 90))
        if resp.status_code not in _RETRYABLE_STATUSES or attempt == _MAX_ATTEMPTS - 1:
            break
        delay = _retry_delay(resp, attempt)
        logger.warning(
            "OpenRouter %s — retrying in %.1fs (attempt %d/%d)",
            resp.status_code, delay, attempt + 1, _MAX_ATTEMPTS,
        )
        time.sleep(delay)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise ValueError(f"OpenRouter error: {body['error']}")
    raw = body["choices"][0]["message"]["content"].strip()
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
    """Generate the phone argumentario for a lead.

    Args:
        lead: Enriched lead dict from web_analyzer, optionally extended with
              ``city`` and ``profession`` from the job payload.

    Returns:
        Dict with a ``phone_script`` key, empty when the model output cannot be
        parsed — the lead is still reported, so it settles rather than holding
        its parent search open over a pitch the agent can write himself.
    """
    name = lead.get("lead", "")

    raw = _complete(_build_prompt(lead))

    try:
        phone_raw = _parse(raw).get("phone_script") or ""
        phone_text = _dict_to_text(phone_raw) if isinstance(phone_raw, dict) else str(phone_raw)
        return {"phone_script": phone_text.replace("\\n", "\n").strip()}
    except (ValueError, KeyError) as exc:
        logger.exception("AI parse failed for '%s': %s — raw: %.200r", name, exc, raw)
        return {"phone_script": ""}
