"""Tests for ai/message_generator.py — pure parsing logic, no real API calls."""

import json
from unittest.mock import MagicMock, patch

import pytest

from ai.message_generator import (
    SYSTEM_PROMPT,
    _PHASES,
    _RESPONSE_FORMAT,
    _build_prompt,
    _dict_to_text,
    _escape_string_newlines,
    _parse,
    _try_load_json,
    generate,
)


# ── JSON string newline escaping ──────────────────────────────────────────────

class TestEscapeStringNewlines:
    def test_literal_newline_inside_string_escaped(self):
        raw = '{"key": "line1\nline2"}'
        result = _escape_string_newlines(raw)
        import json
        parsed = json.loads(result)
        assert parsed["key"] == "line1\nline2"

    def test_structural_newlines_outside_strings_preserved(self):
        raw = '{\n"key": "value"\n}'
        result = _escape_string_newlines(raw)
        assert result == raw

    def test_no_newlines_unchanged(self):
        raw = '{"subject": "Test", "body": "Hello"}'
        assert _escape_string_newlines(raw) == raw

    def test_escaped_backslash_not_confused_with_quote(self):
        raw = r'{"key": "she said \"hello\""}'
        assert _escape_string_newlines(raw) == raw


# ── JSON loading with repair ──────────────────────────────────────────────────

class TestTryLoadJson:
    def test_valid_json(self):
        result = _try_load_json('{"subject": "Hola", "body": "Texto"}')
        assert result == {"subject": "Hola", "body": "Texto"}

    def test_json_with_literal_newlines_in_string(self):
        raw = '{"body": "Línea 1\nLínea 2"}'
        result = _try_load_json(raw)
        assert result is not None
        assert "Línea 1" in result["body"]

    def test_completely_broken_json_returns_none(self):
        assert _try_load_json("esto no es json") is None

    def test_empty_object(self):
        assert _try_load_json("{}") == {}


# ── JSON extraction from raw LLM output ──────────────────────────────────────

class TestParse:
    def test_clean_json_object(self):
        raw = '{"subject": "Asunto", "body": "Cuerpo", "phone_script": "Script"}'
        result = _parse(raw)
        assert result["subject"] == "Asunto"

    def test_json_with_surrounding_text(self):
        raw = 'Aquí va el JSON: {"subject": "Asunto"} — fin'
        result = _parse(raw)
        assert result["subject"] == "Asunto"

    def test_no_json_raises_value_error(self):
        with pytest.raises(ValueError):
            _parse("Esto no contiene ningún objeto JSON")

    def test_uses_outermost_braces(self):
        # The function uses first { and last } — handles nested objects
        raw = '{"outer": {"inner": 1}}'
        result = _parse(raw)
        assert result["outer"] == {"inner": 1}


# ── Dict to plain text ────────────────────────────────────────────────────────

class TestDictToText:
    def test_flat_dict(self):
        d = {"Apertura": "Buenos días", "Cierre": "Hasta luego"}
        text = _dict_to_text(d)
        assert "--- Apertura ---" in text
        assert "Buenos días" in text

    def test_nested_dict(self):
        d = {"Objeciones": {"Precio alto": "Ofrecemos financiación"}}
        text = _dict_to_text(d)
        assert "· Precio alto: Ofrecemos financiación" in text

    def test_list_of_pairs_is_read_aloud_not_dumped(self):
        # The shape the model actually returns for Objeciones. It used to fall through
        # to str(), printing a Python repr into the middle of the call script.
        d = {"Objeciones": [
            {"objeción": "Ya tenemos quien la gestiona", "respuesta": "Es una segunda opinión"},
        ]}
        text = _dict_to_text(d)
        assert "· objeción: Ya tenemos quien la gestiona — respuesta: Es una segunda opinión" in text
        assert "{" not in text and "'" not in text

    def test_list_of_plain_strings(self):
        text = _dict_to_text({"Apertura": ["Buenos días", "Le llamo de SegurSEO"]})
        assert "· Buenos días" in text and "· Le llamo de SegurSEO" in text


# ── generate() end-to-end (mocked API) ───────────────────────────────────────

_VALID_RESPONSE = '{"phone_script": "Apertura: Buenos días...\\nHemos analizado su web..."}'


class TestGenerate:
    @patch("ai.message_generator._complete", return_value=_VALID_RESPONSE)
    def test_returns_only_the_phone_script(self, _):
        # The email draft is gone: anything else here would reach a payload that
        # no longer has a field for it.
        result = generate({"lead": "Test", "has_website": True})
        assert set(result) == {"phone_script"}

    @patch("ai.message_generator._complete", return_value=_VALID_RESPONSE)
    def test_newline_escape_sequences_expanded(self, _):
        assert "\n" in generate({"lead": "Test", "has_website": True})["phone_script"]

    @patch("ai.message_generator._complete", return_value="esto no es json")
    def test_parse_failure_returns_an_empty_script(self, _):
        # Reported as "" rather than raised: the lead settles instead of holding
        # its parent search open over a pitch the agent can write himself.
        assert generate({"lead": "Negocio Test", "has_website": True}) == {"phone_script": ""}

    @patch("ai.message_generator._complete",
           return_value='{"phone_script": {"Apertura": "Buenos días", "Cierre": "Gracias"}}')
    def test_a_structured_script_is_flattened_to_text(self, _):
        script = generate({"lead": "Test", "has_website": True})["phone_script"]
        assert "--- Apertura ---" in script and "Buenos días" in script

    @patch("ai.message_generator._complete", return_value=(
        '{"phone_script": {"Objeciones": {'
        '"Objeción 1": "No estoy seguro", "Respuesta": "Le explico por qué", '
        '"Objeción 2": "Ya tenemos web", "Respuesta": "Es genial que la tenga"}}}'
    ))
    def test_repeated_labels_keep_every_answer(self, _):
        # Verbatim shape from a production lead: a dict kept only the last "Respuesta",
        # losing the first answer and pairing the second with the wrong objection.
        script = generate({"lead": "Test", "has_website": True})["phone_script"]
        assert "· Objeción 1: No estoy seguro — Respuesta: Le explico por qué" in script
        assert "· Objeción 2: Ya tenemos web — Respuesta: Es genial que la tenga" in script

    @patch("ai.message_generator._complete", return_value=(
        '{"phone_script": {"Objeciones": {'
        '"Objeción 1": "No creo que sea tan grave", "Respuesta 1": "Es una obligación legal", '
        '"Objeción 2": "Ya tengo quien la gestiona", "Respuesta 2": "Trabajamos con su equipo"}}}'
    ))
    def test_numbered_labels_are_paired(self, _):
        # Verbatim shape from a production lead: no key repeats, so only ignoring the
        # numbering shows these are labels taking turns.
        script = generate({"lead": "Test", "has_website": True})["phone_script"]
        assert "· Objeción 1: No creo que sea tan grave — Respuesta 1: Es una obligación legal" in script
        assert "· Objeción 2: Ya tengo quien la gestiona — Respuesta 2: Trabajamos con su equipo" in script


# ── What the model is told about the card ─────────────────────────────────────

class TestMapsFindingsReachThePrompt:
    def test_the_social_as_website_label_travels_in_the_maps_problems(self):
        # The finding reaches the model as the label the panel shows, like every other
        # Maps finding: nothing in the prompt names the key.
        label = "La ficha de Google Maps enlaza a LinkedIn en lugar de a su web"
        data = _build_prompt({"lead": "Nobelia Textil", "has_website": True,
                              "maps_issues": {"social_as_website": label}})
        assert label in json.loads(data)["problemas_google_maps"]

    def test_the_system_prompt_tells_the_model_what_to_do_with_it(self):
        # Without this the finding arrived as one more line of the Maps list and the
        # pitch skipped it, which is how the incident went unsaid on the call.
        assert "enlaza a una red social" in SYSTEM_PROMPT
        assert "la propuesta es enlazar la ficha a su web" in SYSTEM_PROMPT


# ── The shape the answer must have ────────────────────────────────────────────

class TestResponseFormat:
    def test_the_format_the_prompt_asks_for_reads_as_pairs(self):
        # The contract and the parser have to agree: the example the model is shown,
        # returned as is, must come out as one line per objection with both voices.
        # Two incidents came from the model inventing the shape of Objeciones.
        example = SYSTEM_PROMPT.split("No numeres ni etiquetes", 1)[1]
        script = _dict_to_text(_parse(example)["phone_script"])
        assert "--- Objeciones ---" in script
        assert "· objeción: lo que dice el cliente — respuesta: lo que respondemos" in script
        assert script.count("--- ") == 5

    def test_the_schema_and_the_prompt_name_the_same_phases(self):
        # One contract described twice: the example the model reads and the schema the
        # provider enforces. If they disagree the model is told one thing and held to another.
        example = SYSTEM_PROMPT.split("No numeres ni etiquetes", 1)[1]
        schema = _RESPONSE_FORMAT["json_schema"]["schema"]["properties"]["phone_script"]
        assert list(_parse(example)["phone_script"]) == list(_PHASES) == schema["required"]

    def test_the_request_asks_for_the_schema_and_a_provider_that_enforces_it(self):
        resp = MagicMock(status_code=200, headers={})
        resp.json.return_value = {"choices": [{"message": {"content": '{"phone_script": "x"}'}}]}
        with patch("ai.message_generator.requests.post", return_value=resp) as post:
            generate({"lead": "Test", "has_website": True})

        payload = post.call_args.kwargs["json"]
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        # Without it OpenRouter may route to a provider that ignores the schema silently.
        assert payload["provider"] == {"require_parameters": True}
