import json
import re

import torch
from transformers import BitsAndBytesConfig, pipeline

from config import LLM_MODEL, SENDER_COMPANY, SENDER_NAME

# Maps individual issue keys to business-impact blocks.
# The model only ever sees the block label, never the technical issue name.
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

_SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "twitter.com", "linkedin.com", "tiktok.com", "youtube.com")

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


def _strip_signature(text: str) -> str:
    """Remove any LLM-generated signature block from the middle paragraph."""
    lines = text.rstrip().splitlines()
    while lines and any(lines[-1].lower().strip().startswith(p) for p in _SIGNATURE_PATTERNS):
        lines.pop()
    while lines and (SENDER_NAME in lines[-1] or SENDER_COMPANY in lines[-1]):
        lines.pop()
    return "\n".join(lines).strip()


def _active_groups(seo_issues: str) -> list[str]:
    """Return business-language descriptions for groups with at least one active issue."""
    active = set(seo_issues.split("|")) if seo_issues else set()
    return [data["label"] for data in _ISSUE_GROUPS.values() if active & data["issues"]]


def _build_prompt(lead: dict) -> str:
    """Build the user prompt describing the lead's situation."""
    name = lead.get("name", "")
    city = lead.get("city", "").replace("*", "").strip()
    website = lead.get("website", "")
    cms = lead.get("cms", "")
    groups = _active_groups(lead.get("seo_issues", ""))
    is_social = any(d in website for d in _SOCIAL_DOMAINS)

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

    Structural whitespace between fields is left untouched so the JSON stays valid.
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


_pipe = None


def _load_model():
    global _pipe
    if _pipe is not None:
        return _pipe

    print(f"[+] Loading {LLM_MODEL} (first run downloads ~15GB)...")

    # 4-bit quantization fits 7B model within 8GB VRAM
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
    )

    _pipe = pipeline(
        "text-generation",
        model=LLM_MODEL,
        model_kwargs={"quantization_config": bnb_config},
        device_map="auto",
    )
    print("[+] Model ready.")
    return _pipe


def generate(lead: dict) -> dict:
    """Generate a personalized outreach email for a lead.

    Args:
        lead: Enriched lead dict from web_analyzer.

    Returns:
        Dict with keys: subject, body.
    """
    pipe = _load_model()

    output = pipe(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(lead)},
        ],
        max_new_tokens=200,
        temperature=0.5,
        do_sample=True,
        pad_token_id=pipe.tokenizer.eos_token_id,
        return_full_text=False,
    )

    text = output[0]["generated_text"].strip()
    name = lead.get("name", "")

    def _assemble(middle: str) -> str:
        return "\n\n".join([
            f"Estimado equipo de {name},",
            _INTRO,
            _strip_signature(middle),
            _AUDIT_OFFER,
        ]) + _SIGNATURE

    def _fallback_middle() -> str:
        return (
            "Hemos detectado en su web algunas áreas de mejora que podrían estar "
            "afectando a su imagen y visibilidad ante sus clientes potenciales."
        )

    def _parse(raw: str) -> dict:
        """Merge all flat JSON objects found in raw; fall back to full-span parse."""
        merged: dict = {}
        for m in re.finditer(r'\{[^{}]+\}', raw, re.DOTALL):
            blob = m.group()
            for candidate in (blob, _escape_string_newlines(blob)):
                try:
                    merged.update(json.loads(candidate))
                    break
                except json.JSONDecodeError:
                    pass
        if merged:
            return merged
        # Full-span fallback (handles single well-formed object)
        start, end = raw.find("{"), raw.rfind("}") + 1
        json_str = raw[start:end]
        for candidate in (json_str, _escape_string_newlines(json_str)):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        raise ValueError("No valid JSON found")

    try:
        parsed = _parse(text)
        middle = parsed.get("body", "")
        if isinstance(middle, dict):
            middle = middle.get("text", middle.get("content", str(middle)))
        middle = middle.replace("\\n", "\n").strip()
        if not middle:
            middle = _fallback_middle()
        subject = parsed.get("subject", "").strip() or f"Propuesta de mejora web para {name}"
        return {"subject": subject, "body": _assemble(middle)}
    except Exception:
        return {
            "subject": f"Propuesta de mejora web para {name}",
            "body": _assemble(_fallback_middle()),
        }
