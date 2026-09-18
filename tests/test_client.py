"""Tests for api/client.py — the worker contract's request layer, no network."""

from unittest.mock import MagicMock, patch

import pytest
import requests

import api.client as client
from api.client import MAX_ERROR_MESSAGE, ReportRejected


def _response(status: int = 200, body: str = "", data=None) -> MagicMock:
    resp = MagicMock()
    resp.ok = 200 <= status < 400
    resp.status_code = status
    resp.text = body
    resp.json.return_value = {"data": data}
    return resp


class TestRequestLayer:
    """One helper builds every call, which is what makes a failure diagnosable."""

    def test_a_failure_carries_the_body_and_the_status(self):
        # raise_for_status() threw the body away, and the body is where the API names the
        # field it refused — so a 422 used to arrive as a status and nothing to act on.
        # Asserted on complete_search_job because it is one of the calls that had no
        # special handling of its own, which is the point of doing this in one place.
        failure = _response(422, '{"message":"El recuento es obligatorio","code":"validation_failed"}')

        with patch.object(client._SESSION, "request", return_value=failure):
            with pytest.raises(requests.HTTPError) as raised:
                client.complete_search_job("s1", 3)

        assert "422" in str(raised.value)
        assert "validation_failed" in str(raised.value)
        assert raised.value.response.status_code == 422

    def test_a_refused_analysis_report_is_its_own_exception(self):
        # So the caller can tell "my payload is wrong" from "the lead could not be
        # analysed", which is what stops a good lead being retired over the former.
        with patch.object(client._SESSION, "request", return_value=_response(422, "{}")):
            with pytest.raises(ReportRejected):
                client.report_analysis("l1", {"cms": "wordpress"})

    def test_every_other_refusal_stays_a_plain_http_error(self):
        with patch.object(client._SESSION, "request", return_value=_response(500, "boom")):
            with pytest.raises(requests.HTTPError) as raised:
                client.report_leads("s1", [])

        assert not isinstance(raised.value, ReportRejected)

    def test_a_claim_returns_the_data_envelope(self):
        with patch.object(client._SESSION, "request", return_value=_response(data={"id": "s1"})):
            assert client.claim_next_search_job() == {"id": "s1"}

    def test_an_empty_queue_answers_none(self):
        with patch.object(client._SESSION, "request", return_value=_response(data=None)):
            assert client.claim_next_analysis_job() is None

    def test_the_token_travels_once_on_the_session(self):
        # Not per call: the worker asks about every business it extracts, so the headers and
        # the connection are set up once rather than rebuilt per Maps card.
        assert client._SESSION.headers["Authorization"].startswith("Bearer ")


class TestFailureMessage:
    def test_an_error_message_is_cut_to_what_the_api_stores(self):
        # The 422 this avoids is the worst one to earn: the fail call is what marks the
        # search failed, so its own rejection leaves the search looking like a live run.
        with patch.object(client._SESSION, "request", return_value=_response()) as request:
            client.fail_search_job("s1", "E" * 5000)

        assert len(request.call_args.kwargs["json"]["error_message"]) == MAX_ERROR_MESSAGE
