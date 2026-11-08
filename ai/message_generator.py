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
from audit import recorder

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


def _elapsed_ms(since: float) -> int:
    """Milliseconds since a monotonic mark — the clock cannot step mid-call."""
    return round((time.monotonic() - since) * 1000)


def _attempt(status: int | None, started: float) -> dict:
    """One entry of the audited attempts list.

    ``status`` is None when the request never came back at all, which is what tells a
    timeout from a refusal afterwards. ``retry_in_s`` is filled in by the caller, and only
    when a wait actually follows, so the last attempt of a call carries null.
    """
    return {"status": status, "latency_ms": _elapsed_ms(started), "retry_in_s": None}


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

# The phases of the call script, in the order the agent reads them. The response schema is
# built from this list, and a test holds the prompt's example to it: the two describe one
# contract and must not drift apart.
_PHASES = ("Apertura", "Identificación del problema", "Impacto en el negocio", "Propuesta", "Objeciones")


def _response_format() -> dict:
    """The schema OpenRouter makes the model generate against (structured outputs).

    json_object only guaranteed valid JSON, not its shape, and the model filled the gap
    with a different Objeciones layout per call — two of which reached production as
    labels shown as the customer's words. Every phase is a string; the last is the list
    of exchanges, each with the customer's line and ours.
    """
    text = {"type": "string"}
    exchange = {
        "type": "object",
        "properties": {"objeción": text, "respuesta": text},
        "required": ["objeción", "respuesta"],
        "additionalProperties": False,
    }
    phases = {name: text for name in _PHASES}
    phases[_PHASES[-1]] = {"type": "array", "items": exchange}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "argumentario",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"phone_script": {
                    "type": "object",
                    "properties": phases,
                    "required": list(_PHASES),
                    "additionalProperties": False,
                }},
                "required": ["phone_script"],
                "additionalProperties": False,
            },
        },
    }


_RESPONSE_FORMAT = _response_format()

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
- Estructurado en las cinco fases del formato de respuesta, en ese orden
- Frases cortas y directas, listas para leer en voz alta
- Al menos 2 objeciones frecuentes con su respuesta concisa
- Sin emojis, sin exclamaciones

FICHA DE GOOGLE MAPS QUE ENLAZA A UNA RED SOCIAL:
Si entre los `problemas_google_maps` aparece que la ficha enlaza a una red social en lugar
de a su web, es un problema de la ficha, no de la web: quien pulsa «Sitio web» en Google
Maps acaba en un perfil social y no en la web del negocio.
- Si tiene_web es true: la propuesta es enlazar la ficha a su web. El negocio tiene web;
  lo que está mal es a dónde manda Google a sus clientes.
- Si tiene_web es false: súmalo al argumento de no tener web, no lo presentes como un
  problema aparte.
- Preséntalo entre los problemas de la ficha de Google Maps y por delante de problemas SEO
  menores, como las imágenes sin texto alternativo.
- No afirmes efectos concretos sobre el posicionamiento: habla de clientes que buscan el
  negocio y no llegan a su web.

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

Responde ÚNICAMENTE con JSON válido, sin texto adicional, con exactamente esta forma.
Cada fase es un texto; Objeciones es una lista en la que cada elemento lleva lo que dice
el cliente en "objeción" y lo que respondemos en "respuesta". No numeres ni etiquetes
las objeciones de ninguna otra forma.
{{"phone_script": {{
  "Apertura": "texto",
  "Identificación del problema": "texto",
  "Impacto en el negocio": "texto",
  "Propuesta": "texto",
  "Objeciones": [
    {{"objeción": "lo que dice el cliente", "respuesta": "lo que respondemos"}},
    {{"objeción": "lo que dice el cliente", "respuesta": "lo que respondemos"}}
  ]
}}}}"""


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


def _correction(errors: list[str]) -> str:
    """The second user turn: what was wrong with the answer, and what to send instead."""
    problems = "\n".join(f"- {error}" for error in errors)
    return (f"El JSON anterior no tiene la forma pedida:\n{problems}\n\n"
            "Devuelve el JSON completo corregido, con exactamente la forma indicada, "
            "sin texto adicional.")


def _complete(user_prompt: str, *, rejected: str = "", errors: list[str] | None = None) -> str:
    """Send a chat completion request to OpenRouter and return the raw text.

    Retries transient failures (rate limits, upstream 5xx) up to _MAX_ATTEMPTS
    times using server-hinted or exponential-backoff delays. 402 is never
    retried — it signals a billing issue handled upstream by the worker.

    On a corrective attempt the request becomes a conversation: the original prompt, the
    answer that did not fit as the model's own turn, and what was wrong with it. That is
    the only difference — the HTTP retries above are a separate matter and unchanged, and
    a 402 or a timeout still propagates rather than being retried for its format.

    Args:
        user_prompt: What the model is asked for.
        rejected: The answer being corrected, sent back as the assistant's turn.
        errors: What was wrong with it, which is what the correction asks it to fix.

    Audited from a ``finally``, so a call that timed out or was refused leaves the same
    trace as one that answered: what was attempted, and how long each attempt took. The
    headers are never handed to the recorder — the API key is in them — and nothing here
    changes what is sent, including the fact that OpenRouter only returns a cost when the
    request asks for one.
    """
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    # Recorded as one text, so the audited call can be read on its own: a corrective
    # attempt whose prompt was only the correction would say nothing about the lead.
    audited_prompt = user_prompt
    if errors:
        correction = _correction(errors)
        messages += [{"role": "assistant", "content": rejected},
                     {"role": "user", "content": correction}]
        audited_prompt = f"{user_prompt}\n\n{correction}"

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": 2000,
        "temperature": 0.6,
        "response_format": _RESPONSE_FORMAT,
        # The model is served by several providers and not all of them enforce a schema;
        # one that does not would ignore it silently. This routes only to those that do.
        "provider": {"require_parameters": True},
    }
    recorder.llm_call_started(
        model=payload["model"], system_prompt=SYSTEM_PROMPT, user_prompt=audited_prompt,
        temperature=payload["temperature"], max_tokens=payload["max_tokens"],
    )
    attempts: list[dict] = []
    call_started = time.monotonic()
    usage: dict = {}
    response_raw = None
    try:
        for attempt in range(_MAX_ATTEMPTS):
            attempt_started = time.monotonic()
            try:
                resp = requests.post(_OPENROUTER_URL, headers=headers, json=payload, timeout=(5, 90))
            except Exception:
                attempts.append(_attempt(None, attempt_started))
                raise
            attempts.append(_attempt(resp.status_code, attempt_started))
            if resp.status_code not in _RETRYABLE_STATUSES or attempt == _MAX_ATTEMPTS - 1:
                break
            delay = _retry_delay(resp, attempt)
            attempts[-1]["retry_in_s"] = delay
            logger.warning(
                "OpenRouter %s — retrying in %.1fs (attempt %d/%d)",
                resp.status_code, delay, attempt + 1, _MAX_ATTEMPTS,
            )
            time.sleep(delay)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise ValueError(f"OpenRouter error: {body['error']}")
        # Recorded as the model sent it, before the fences come off, and read only once
        # the body has proved to be the shape this function expects: an exception raised
        # in the finally below would replace the one the caller is meant to see.
        response_raw = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        raw = response_raw.strip()
        # Strip markdown code fences — some models add them despite json_object mode
        if raw.startswith("```"):
            raw = re.sub(r'^```[a-z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw.rstrip()).strip()
        return raw
    finally:
        recorder.llm_call_finished(
            attempts=attempts,
            final_status=attempts[-1]["status"] if attempts else None,
            latency_ms=_elapsed_ms(call_started),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cost_usd=usage.get("cost"),
            response_raw=response_raw,
        )


def _label_form(key: str) -> str:
    """A key with its numbering removed, so "Objeción 2" and "Objeción" read as one label."""
    return re.sub(r"\d+", "", key).strip().casefold()


def _keep_repeated_keys(pairs: list) -> dict | list:
    """Build a JSON object without losing values or pairings to labelled keys.

    The model sometimes labels each line of a section instead of keying it by content,
    with or without numbers: "Objeción 1", "Respuesta", "Objeción 2", "Respuesta", or
    "Objeción 1", "Respuesta 1", "Objeción 2", "Respuesta 2". As a dict, the first shape
    loses every repeated answer but the last; as one line per entry, both shapes put
    the label where the panel expects the customer's words.

    Keys that repeat once their numbering is ignored are labels. When two of them take
    turns, each pair is one exchange and is written "label: text — label: text". Any other
    repetition is kept as one line per entry, in order, so nothing is dropped.

    Since the answer is validated, none of this reaches the agent any more: a labelled
    Objeciones fails the format check and is sent back to be corrected. What this still
    buys is that the check sees every entry. Left to ``dict``, four labelled lines arrive
    as two — the repeats overwrite — and a validator counting objections would be
    counting what Python kept rather than what the model sent.
    """
    forms = [_label_form(k) for k, _ in pairs]
    if len(forms) == len(set(forms)):
        return dict(pairs)

    lines = [f"{k}: {v}" for k, v in pairs]
    turns = list(dict.fromkeys(forms))
    if len(turns) == 2 and all(form == turns[i % 2] for i, form in enumerate(forms)):
        return [" — ".join(lines[i:i + 2]) for i in range(0, len(lines), 2)]
    return lines


def _try_load_json(s: str) -> dict | None:
    """Parse s as JSON, retrying after escaping literal newlines. Returns None on failure."""
    for candidate in (s, _escape_string_newlines(s)):
        try:
            result = json.loads(candidate, object_pairs_hook=_keep_repeated_keys)
        except json.JSONDecodeError:
            continue
        return result if isinstance(result, dict) else None
    return None


def _inline(value) -> str:
    """One value on a single line, a dict of objection/answer included."""
    if isinstance(value, dict):
        return " — ".join(f"{k}: {v}" for k, v in value.items())
    if isinstance(value, list):
        return "; ".join(_inline(v) for v in value)
    return str(value)


def _dict_to_text(d: dict) -> str:
    """Flatten a structured dict (phase label → content) into readable plain text.

    Every shape the model returns has to come out as something an agent can read aloud.
    A list was falling through to ``str()``, which printed a Python repr — quotes,
    braces and all — into the middle of the script, and the Objeciones section is
    precisely where the model likes to return a list of pairs.
    """
    lines = []
    for section, content in d.items():
        lines.append(f"--- {section} ---")
        if isinstance(content, dict):
            lines.extend(f"· {k}: {_inline(v)}" for k, v in content.items())
        elif isinstance(content, list):
            lines.extend(f"· {_inline(item)}" for item in content)
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


# ── The shape the answer must have ────────────────────────────────────────────
#
# The second barrier behind the schema in `_RESPONSE_FORMAT`. OpenRouter is asked to route
# only to providers that enforce one, but a provider that quietly does not would return a
# shape nobody checked, and two of those reached a real lead.
#
# **The messages never quote the model** — not a value, not even the name of a key that
# should not be there. They name the phase, the position or the type, and they are in
# Spanish because the person reading a failed analysis works in Spanish. That is what lets
# the API keep `format_errors` as metadata: it outlives the 90-day purge of the prompt and
# the answer it describes, so "what was wrong with this one" is still answerable when the
# text itself is gone. A message carrying a fragment of the model's output would be
# personal data in a column built to survive.

_NOT_JSON = "La respuesta no es JSON válido."
_OBJECTIONS = _PHASES[-1]
_EXCHANGE_KEYS = ("objeción", "respuesta")
_MIN_OBJECTIONS = 2


def _is_filled(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def _text_error(subject: str, value) -> str:
    """Why a value is not usable text. Feminine: every field named here is."""
    return f"{subject} está vacía." if isinstance(value, str) else f"{subject} no es un texto."


def _objection_errors(objections) -> list[str]:
    """What is wrong with the Objeciones list, by position."""
    if not isinstance(objections, list):
        return [f"«{_OBJECTIONS}» no es una lista."]

    errors = []
    if len(objections) < _MIN_OBJECTIONS:
        errors.append(f"«{_OBJECTIONS}» tiene {_count(len(objections), 'elemento', 'elementos')}; "
                      f"hacen falta al menos {_MIN_OBJECTIONS}.")

    for position, exchange in enumerate(objections, start=1):
        if not isinstance(exchange, dict):
            errors.append(f"La objeción {position} no es un objeto.")
            continue
        subjects = (f"La objeción {position}", f"La respuesta de la objeción {position}")
        for key, subject in zip(_EXCHANGE_KEYS, subjects):
            if key not in exchange:
                errors.append(f"La objeción {position} no tiene «{key}».")
            elif not _is_filled(exchange[key]):
                errors.append(_text_error(subject, exchange[key]))
        if spare := len(set(exchange) - set(_EXCHANGE_KEYS)):
            errors.append(f"La objeción {position} tiene "
                          f"{_count(spare, 'campo no pedido', 'campos no pedidos')}.")
    return errors


def _format_errors(answer) -> list[str]:
    """Every way the answer departs from the shape asked for; empty when it fits.

    Args:
        answer: The parsed answer, or None when there was no JSON to parse.

    Returns:
        One message per problem, in Spanish and quoting nothing the model wrote — see the
        note above this function, which is the reason the API can keep them.
    """
    if answer is None:
        return [_NOT_JSON]
    if not isinstance(answer, dict):
        return ["La respuesta no es un objeto."]
    if "phone_script" not in answer:
        return ["Falta «phone_script»."]

    script = answer["phone_script"]
    if not isinstance(script, dict):
        return ["«phone_script» no es un objeto."]

    errors = [f"Falta la fase «{phase}»." for phase in _PHASES if phase not in script]
    if spare := len(set(script) - set(_PHASES)):
        errors.append(f"Hay {_count(spare, 'fase no pedida', 'fases no pedidas')}.")

    for phase in _PHASES[:-1]:
        if phase in script and not _is_filled(script[phase]):
            errors.append(_text_error(f"La fase «{phase}»", script[phase]))

    if _OBJECTIONS in script:
        errors += _objection_errors(script[_OBJECTIONS])
    return errors


def _loaded(raw: str):
    """The JSON object in the answer, or None when there is none to read."""
    try:
        return _parse(raw)
    except ValueError:
        return None


def _script_text(answer: dict) -> str:
    """The validated script as the text the agent reads aloud."""
    return _dict_to_text(answer["phone_script"]).replace("\\n", "\n").strip()


def generate(lead: dict) -> dict:
    """Generate the phone argumentario for a lead.

    The answer is checked against the shape that was asked for, and one corrective attempt
    is made when it does not fit: the model is shown its own answer and what was wrong with
    it. One, not a loop — an argumentario is worth a second try and not a budget, and the
    agent can write the pitch himself.

    Each attempt is its own audited call, with its own ``format_errors``, so a lead that
    needed correcting is told apart afterwards from one that answered properly first time.

    Args:
        lead: Enriched lead dict from web_analyzer, optionally extended with
              ``city`` and ``profession`` from the job payload.

    Returns:
        Dict with a ``phone_script`` key, empty when neither answer could be used — the
        lead is still reported, so it settles rather than holding its parent search open
        over a pitch the agent can write himself.
    """
    name = lead.get("lead", "")
    prompt = _build_prompt(lead)

    raw = _complete(prompt)
    answer = _loaded(raw)
    if errors := _format_errors(answer):
        recorder.llm_call_parsed({"phone_script": ""}, parsed_ok=False, format_errors=errors)
        logger.warning("AI answer for '%s' did not fit the format (%d problems); asking again",
                       name, len(errors))
        raw = _complete(prompt, rejected=raw, errors=errors)
        answer = _loaded(raw)
        errors = _format_errors(answer)

    if errors:
        # The tokens were spent twice and the lead is still reported: it settles with no
        # pitch rather than holding its parent search open, and the audit says why.
        recorder.llm_call_parsed({"phone_script": ""}, parsed_ok=False, format_errors=errors)
        logger.warning("AI answer for '%s' did not fit the format after a correction (%d "
                       "problems); reporting no pitch", name, len(errors))
        return {"phone_script": ""}

    result = {"phone_script": _script_text(answer)}
    recorder.llm_call_parsed(result, parsed_ok=True, format_errors=None)
    return result
