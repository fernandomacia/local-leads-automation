"""What the audit records about a call to OpenRouter, with requests simulated.

The row is written in three fragments — the call as it opens, the HTTP result, and what
the worker made of the answer — so the assertions read them in that order.
"""

import hashlib
import json
from unittest.mock import patch

import pytest
import requests

from ai.message_generator import SYSTEM_PROMPT, generate
from audit import recorder
from audit.outbox import Outbox
from config import OPENROUTER_API_KEY, OPENROUTER_MODEL

_SCRIPT = '{"phone_script": "Buenos días"}'


def body(content: str = _SCRIPT, usage: dict | None = None) -> dict:
    """An OpenRouter chat-completion body."""
    answer = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        answer["usage"] = usage
    return answer


class FakeResponse:
    """Just enough of ``requests.Response`` for ``_complete`` to work on."""

    def __init__(self, status: int = 200, payload: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else body()

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} from OpenRouter", response=self)


def calls(box: Outbox) -> list[dict]:
    """The fragments written for llm_calls, in the order they were written."""
    return [record.fields for record in box.pending(1000) if record.target == "llm_calls"]


@pytest.fixture
def audited(tmp_path):
    """A run with an analysis in progress, writing to a temporary outbox."""
    box = Outbox(tmp_path / "llm-outbox.sqlite")
    recorder.start(outbox=box, dsn="")
    recorder.analysis_started({"id": "lead-1", "business_name": "Ejemplo SL"})
    yield box
    recorder.stop("stopped")


def ask() -> dict:
    return generate({"lead": "Ejemplo SL", "has_website": True})


# ── A call that answered ──────────────────────────────────────────────────────

class TestACallThatAnswered:
    def test_the_prompts_the_tokens_and_the_answer_are_recorded(self, audited):
        with patch("ai.message_generator.requests.post",
                   return_value=FakeResponse(payload=body(usage={"prompt_tokens": 1200,
                                                              "completion_tokens": 340}))):
            result = ask()

        opened, http, parsed = calls(audited)
        assert opened["model"] == OPENROUTER_MODEL
        assert opened["temperature"] == 0.6
        assert opened["max_tokens"] == 2000
        assert opened["system_prompt"] == SYSTEM_PROMPT
        # Kept beside the prompt because it outlives it: the text is cleared after the
        # retention window, so the hash is what still answers "which prompt wrote this".
        assert opened["system_prompt_sha256"] == hashlib.sha256(
            SYSTEM_PROMPT.encode("utf-8")).hexdigest()
        assert "Ejemplo SL" in opened["user_prompt"]

        assert [attempt["status"] for attempt in http["attempts"]] == [200]
        assert http["final_status"] == 200
        assert http["prompt_tokens"] == 1200
        assert http["completion_tokens"] == 340
        # OpenRouter answers with a cost only when the request asks for one, and asking
        # would mean changing what is sent for the audit's benefit.
        assert http["cost_usd"] is None
        assert http["response_raw"] == _SCRIPT

        assert parsed["parsed_ok"] is True
        assert parsed["response_parsed"] == result

    def test_the_answer_is_kept_before_the_fences_come_off(self, audited):
        fenced = f"```json\n{_SCRIPT}\n```"
        with patch("ai.message_generator.requests.post",
                   return_value=FakeResponse(payload=body(fenced))):
            assert ask() == {"phone_script": "Buenos días"}

        assert calls(audited)[1]["response_raw"] == fenced

    def test_neither_the_headers_nor_the_key_are_recorded(self, audited):
        with patch("ai.message_generator.requests.post", return_value=FakeResponse(payload=body())):
            ask()

        written = json.dumps(calls(audited), default=str)
        assert OPENROUTER_API_KEY not in written
        assert "Authorization" not in written


# ── A call that had to be retried ─────────────────────────────────────────────

class TestARetriedCall:
    def test_both_attempts_are_recorded_with_the_wait_between_them(self, audited):
        with patch("ai.message_generator.requests.post",
                   side_effect=[FakeResponse(status=429, headers={"Retry-After": "2"}),
                                FakeResponse(payload=body())]), \
             patch("ai.message_generator.time.sleep") as sleep:
            ask()

        http = calls(audited)[1]
        assert [attempt["status"] for attempt in http["attempts"]] == [429, 200]
        # The wait that was actually applied, not the one a formula would suggest.
        assert http["attempts"][0]["retry_in_s"] == 2.0
        # Nothing followed the last attempt, so it waited for nothing.
        assert http["attempts"][1]["retry_in_s"] is None
        assert http["final_status"] == 200
        sleep.assert_called_once_with(2.0)


# ── An answer that could not be used ──────────────────────────────────────────

class TestAnAnswerThatCouldNotBeParsed:
    def test_the_call_is_recorded_as_a_success_and_the_answer_as_a_failure(self, audited):
        with patch("ai.message_generator.requests.post",
                   return_value=FakeResponse(payload=body("esto no es json"))):
            assert ask() == {"phone_script": ""}

        _, http, parsed = calls(audited)
        # The two are told apart here because nothing downstream can: the request worked,
        # the tokens were spent, and the lead is reported with an empty pitch.
        assert http["final_status"] == 200
        assert http["response_raw"] == "esto no es json"
        assert parsed["parsed_ok"] is False
        assert parsed["response_parsed"] == {"phone_script": ""}


# ── A call that failed ────────────────────────────────────────────────────────

class TestACallThatFailed:
    def test_a_request_that_never_came_back_is_recorded_as_an_attempt(self, audited):
        with patch("ai.message_generator.requests.post", side_effect=requests.Timeout("too slow")):
            with pytest.raises(requests.Timeout):
                ask()

        opened, http = calls(audited)
        # No status, which is what tells a timeout from a refusal afterwards.
        assert [attempt["status"] for attempt in http["attempts"]] == [None]
        assert http["final_status"] is None
        assert http["response_raw"] is None

    def test_a_refused_call_keeps_the_status_it_was_refused_with(self, audited):
        with patch("ai.message_generator.requests.post", return_value=FakeResponse(status=402)):
            with pytest.raises(requests.HTTPError):
                ask()

        written = calls(audited)
        assert written[1]["final_status"] == 402
        # Two fragments, not three: there was no answer to make anything of.
        assert len(written) == 2

    def test_the_exception_reaches_the_caller_unchanged(self, audited):
        # The worker reads 402 and 429 off this exception to decide whether the lead goes
        # back to the queue, so the audit must not stand between the two.
        error = requests.ConnectionError("API down")
        with patch("ai.message_generator.requests.post", side_effect=error):
            with pytest.raises(requests.ConnectionError) as raised:
                ask()

        assert raised.value is error


# ── Outside an analysis ───────────────────────────────────────────────────────

class TestWithoutAnAnalysis:
    def test_nothing_is_recorded(self, tmp_path):
        # llm_calls.analysis_id is NOT NULL, so a call made with no lead in hand has no
        # row to hang off: leaving it unrecorded beats a fragment the store refuses.
        box = Outbox(tmp_path / "llm-outbox.sqlite")
        recorder.start(outbox=box, dsn="")
        try:
            with patch("ai.message_generator.requests.post",
                       return_value=FakeResponse(payload=body())):
                assert ask() == {"phone_script": "Buenos días"}
            assert calls(box) == []
        finally:
            recorder.stop("stopped")
