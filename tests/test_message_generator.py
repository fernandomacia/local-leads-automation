"""Tests for ai/message_generator.py — pure parsing logic, no real API calls."""

from unittest.mock import patch

import pytest

from ai.message_generator import (
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


# ── generate() end-to-end (mocked API) ───────────────────────────────────────

_VALID_RESPONSE = (
    '{"subject": "Mejora tu web", '
    '"body": "Estimados señores,\\nHemos analizado su web...", '
    '"phone_script": "Apertura: Buenos días..."}'
)


class TestGenerate:
    @patch("ai.message_generator._complete", return_value=_VALID_RESPONSE)
    def test_returns_all_keys(self, _):
        result = generate({"lead": "Test", "has_website": True})
        assert set(result.keys()) >= {"subject", "body", "phone_script"}

    @patch("ai.message_generator._complete", return_value=_VALID_RESPONSE)
    def test_newline_escape_sequences_expanded(self, _):
        result = generate({"lead": "Test", "has_website": True})
        assert "\n" in result["body"]

    @patch("ai.message_generator._complete", return_value="esto no es json")
    def test_parse_failure_returns_safe_fallback(self, _):
        result = generate({"lead": "Negocio Test", "has_website": True})
        assert result["body"] == ""
        assert result["phone_script"] == ""
        assert "Negocio Test" in result["subject"]

    @patch("ai.message_generator._complete",
           return_value='{"subject": "", "body": "", "phone_script": "Argumentario"}')
    def test_no_website_no_email_empty_subject_and_body(self, _):
        result = generate({"lead": "Test", "has_website": False, "email": ""})
        assert result["subject"] == ""
        assert result["body"] == ""
        assert result["phone_script"] == "Argumentario"
