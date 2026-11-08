"""Tests for ai/message_generator.py — pure parsing logic, no real API calls."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from ai.message_generator import (
    SYSTEM_PROMPT,
    _PHASES,
    _RESPONSE_FORMAT,
    _build_prompt,
    _dict_to_text,
    _escape_string_newlines,
    _format_errors,
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

_PITCH = {
    "Apertura": "Buenos días, le llamo de SegurSEO.",
    "Identificación del problema": "Su web no carga en móvil.",
    "Impacto en el negocio": "Pierde a quien la busca desde el teléfono.",
    "Propuesta": "Lo dejamos resuelto en una semana.",
    "Objeciones": [
        {"objeción": "No estoy seguro", "respuesta": "Le explico por qué"},
        {"objeción": "Ya tenemos web", "respuesta": "Es genial que la tenga"},
    ],
}


def answer(**phases) -> str:
    """A complete argumentario as the model is told to return it, phases overridden."""
    return json.dumps({"phone_script": {**_PITCH, **phases}}, ensure_ascii=False)


_VALID_RESPONSE = answer()


class TestGenerate:
    @patch("ai.message_generator._complete", return_value=_VALID_RESPONSE)
    def test_returns_only_the_phone_script(self, _):
        # The email draft is gone: anything else here would reach a payload that
        # no longer has a field for it.
        result = generate({"lead": "Test", "has_website": True})
        assert set(result) == {"phone_script"}

    @patch("ai.message_generator._complete",
           return_value=answer(Apertura="Buenos días.\\nHemos analizado su web."))
    def test_newline_escape_sequences_expanded(self, _):
        script = generate({"lead": "Test", "has_website": True})["phone_script"]
        assert "Buenos días.\nHemos analizado su web." in script

    @patch("ai.message_generator._complete", return_value="esto no es json")
    def test_parse_failure_returns_an_empty_script(self, _):
        # Reported as "" rather than raised: the lead settles instead of holding
        # its parent search open over a pitch the agent can write himself.
        assert generate({"lead": "Negocio Test", "has_website": True}) == {"phone_script": ""}

    @patch("ai.message_generator._complete", return_value=_VALID_RESPONSE)
    def test_a_structured_script_is_flattened_to_text(self, _):
        script = generate({"lead": "Test", "has_website": True})["phone_script"]
        assert "--- Apertura ---" in script
        assert "Buenos días, le llamo de SegurSEO." in script
        # Each objection on one line, both voices labelled as the panel splits them.
        assert "· objeción: No estoy seguro — respuesta: Le explico por qué" in script


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


# ── Checking the answer against that shape ────────────────────────────────────

# The two shapes that actually reached a lead, verbatim. Neither is a list of exchanges,
# so both are now caught before the agent reads anything.
_REPEATED_LABELS = ('{"phone_script": {"Objeciones": {'
                    '"Objeción 1": "No estoy seguro", "Respuesta": "Le explico por qué", '
                    '"Objeción 2": "Ya tenemos web", "Respuesta": "Es genial que la tenga"}}}')
_NUMBERED_LABELS = ('{"phone_script": {"Objeciones": {'
                    '"Objeción 1": "No creo que sea tan grave", '
                    '"Respuesta 1": "Es una obligación legal", '
                    '"Objeción 2": "Ya tengo quien la gestiona", '
                    '"Respuesta 2": "Trabajamos con su equipo"}}}')


class TestFormatErrors:
    def test_the_shape_asked_for_passes(self):
        assert _format_errors(_parse(_VALID_RESPONSE)) == []

    def test_an_answer_that_is_not_json_says_only_that(self):
        assert _format_errors(None) == ["La respuesta no es JSON válido."]

    @pytest.mark.parametrize("answer, expected", [
        ("no un objeto", "La respuesta no es un objeto."),
        ({}, "Falta «phone_script»."),
        ({"phone_script": "un texto"}, "«phone_script» no es un objeto."),
    ])
    def test_the_outer_shape(self, answer, expected):
        assert _format_errors(answer) == [expected]

    def test_a_missing_phase_is_named(self):
        script = {phase: "texto" for phase in _PHASES if phase != "Propuesta"}
        script["Objeciones"] = _PITCH["Objeciones"]
        assert "Falta la fase «Propuesta»." in _format_errors({"phone_script": script})

    def test_phases_that_were_not_asked_for_are_counted_not_named(self):
        errors = _format_errors(json.loads(answer(Cierre="Gracias", Despedida="Adiós")))
        assert errors == ["Hay 2 fases no pedidas."]

    @pytest.mark.parametrize("value, expected", [
        ("", "La fase «Propuesta» está vacía."),
        ("   ", "La fase «Propuesta» está vacía."),
        (42, "La fase «Propuesta» no es un texto."),
    ])
    def test_a_phase_has_to_be_text_with_something_in_it(self, value, expected):
        assert _format_errors(json.loads(answer(Propuesta=value))) == [expected]

    def test_objections_have_to_be_a_list_of_at_least_two(self):
        assert _format_errors(json.loads(answer(Objeciones={}))) == ["«Objeciones» no es una lista."]
        one = [{"objeción": "sola", "respuesta": "una"}]
        assert ("«Objeciones» tiene 1 elemento; hacen falta al menos 2."
                in _format_errors(json.loads(answer(Objeciones=one))))

    @pytest.mark.parametrize("exchange, expected", [
        ("un texto", "La objeción 1 no es un objeto."),
        ({"respuesta": "sin objeción"}, "La objeción 1 no tiene «objeción»."),
        ({"objeción": "sin respuesta"}, "La objeción 1 no tiene «respuesta»."),
        ({"objeción": "", "respuesta": "vacía"}, "La objeción 1 está vacía."),
        ({"objeción": "x", "respuesta": ""}, "La respuesta de la objeción 1 está vacía."),
        ({"objeción": "x", "respuesta": 7}, "La respuesta de la objeción 1 no es un texto."),
        ({"objeción": "x", "respuesta": "y", "nota": "z"},
         "La objeción 1 tiene 1 campo no pedido."),
    ])
    def test_each_exchange_is_judged_by_its_position(self, exchange, expected):
        errors = _format_errors(json.loads(answer(Objeciones=[exchange, exchange])))
        assert expected in errors

    @pytest.mark.parametrize("shape", [_REPEATED_LABELS, _NUMBERED_LABELS])
    def test_the_shapes_that_reached_a_lead_are_caught(self, shape):
        errors = _format_errors(_parse(shape))
        # This is what _keep_repeated_keys buys now: it paired the labels into one line
        # per exchange, so the check sees both of them and says neither is an object.
        # Left to `dict`, the repeats would overwrite and half the entries would be gone
        # before anything looked at them.
        assert "La objeción 1 no es un objeto." in errors
        assert "La objeción 2 no es un objeto." in errors
        assert "Falta la fase «Apertura»." in errors

    def test_the_errors_quote_nothing_the_model_wrote(self):
        # The reason the API can keep these: they outlive the purge that empties the
        # prompt and the answer, so a fragment of the model's text would be personal data
        # in a column built to survive. Not even the name of a key it invented.
        secret = "Panadería Zzyzx del Carmen"
        reply = json.dumps({"phone_script": {
            **_PITCH,
            secret: "una fase que no pedimos",
            "Propuesta": {"nota": secret},
            "Objeciones": [{"objeción": "x", "respuesta": "", secret: secret},
                           {"objeción": "y", "respuesta": "z"}],
        }}, ensure_ascii=False)

        errors = _format_errors(json.loads(reply))
        assert len(errors) == 4
        assert not any(secret in error for error in errors)


# ── One corrective attempt, and only one ──────────────────────────────────────

def _response(status: int = 200, content: str = "") -> MagicMock:
    """Just enough of a chat-completion response for _complete to work on."""
    response = MagicMock(status_code=status, headers={})
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
    return response


def _send(*answers: str) -> tuple[dict, list[dict]]:
    """Drive generate() against these answers in order; return it and what was posted."""
    with patch("ai.message_generator.requests.post",
               side_effect=[_response(content=a) for a in answers]) as post:
        result = generate({"lead": "Ejemplo SL", "has_website": True})
    return result, [call.kwargs["json"] for call in post.call_args_list]


class TestTheCorrectiveAttempt:
    def test_an_answer_that_fits_is_asked_for_once(self):
        result, sent = _send(_VALID_RESPONSE)
        assert len(sent) == 1
        assert "Buenos días, le llamo de SegurSEO." in result["phone_script"]

    def test_an_answer_that_does_not_fit_is_sent_back_with_what_was_wrong(self):
        rejected = answer(Propuesta="")
        result, sent = _send(rejected, _VALID_RESPONSE)

        assert len(sent) == 2
        # A conversation, not a fresh ask: the model is shown its own answer and the
        # problem with it, so the correction is of that answer rather than a second guess.
        assert [message["role"] for message in sent[1]["messages"]] == [
            "system", "user", "assistant", "user"]
        assert sent[1]["messages"][1] == sent[0]["messages"][1]
        assert sent[1]["messages"][2]["content"] == rejected
        correction = sent[1]["messages"][3]["content"]
        assert "La fase «Propuesta» está vacía." in correction
        assert "Devuelve el JSON completo corregido" in correction
        # And the corrected answer is what the agent gets.
        assert "Lo dejamos resuelto en una semana." in result["phone_script"]

    def test_an_answer_that_is_not_json_is_corrected_too(self):
        result, sent = _send("esto no es json", _VALID_RESPONSE)
        assert len(sent) == 2
        assert "La respuesta no es JSON válido." in sent[1]["messages"][3]["content"]
        assert "Buenos días, le llamo de SegurSEO." in result["phone_script"]

    @pytest.mark.parametrize("shape", [_REPEATED_LABELS, _NUMBERED_LABELS])
    def test_the_shapes_that_reached_a_lead_are_asked_again(self, shape):
        result, sent = _send(shape, _VALID_RESPONSE)
        assert len(sent) == 2
        assert "Buenos días, le llamo de SegurSEO." in result["phone_script"]

    def test_two_answers_that_do_not_fit_leave_no_pitch(self):
        # The tokens were spent twice and the lead is still reported: it settles with no
        # pitch rather than holding its parent search open, and the audit says why.
        result, sent = _send(answer(Objeciones=[]), answer(Objeciones=[]))
        assert len(sent) == 2
        assert result == {"phone_script": ""}

    def test_there_is_no_third_attempt(self):
        # One correction, not a budget: an argumentario is worth a second try, and the
        # agent can write the pitch himself.
        with patch("ai.message_generator.requests.post",
                   side_effect=[_response(content="no json"), _response(content="no json"),
                                _response(content=_VALID_RESPONSE)]) as post:
            assert generate({"lead": "Ejemplo SL", "has_website": True}) == {"phone_script": ""}
        assert post.call_count == 2


class TestAnHttpFailureIsNotAFormatProblem:
    """A 402 or a timeout says nothing about the shape, and the worker reads them itself."""

    def test_a_402_propagates_without_a_second_attempt(self):
        with patch("ai.message_generator.requests.post", return_value=_response(status=402)) as post:
            with pytest.raises(requests.HTTPError):
                generate({"lead": "Ejemplo SL", "has_website": True})

        assert post.call_count == 1

    def test_a_timeout_propagates_without_a_second_attempt(self):
        with patch("ai.message_generator.requests.post", side_effect=requests.Timeout) as post:
            with pytest.raises(requests.Timeout):
                generate({"lead": "Ejemplo SL", "has_website": True})

        assert post.call_count == 1
