"""Shared fixtures for the test suite.

Env vars are set before any project module is imported so config.py
validation passes even without a real .env file (e.g. in CI).
Real .env values always take precedence via load_dotenv(override=True).
"""

import os
os.environ.setdefault("API_BASE_URL", "http://test.local")
os.environ.setdefault("API_TOKEN", "test-token")
os.environ.setdefault("SENDER_COMPANY", "Test Company SL")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

import pytest
from bs4 import BeautifulSoup

from audit import recorder


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


@pytest.fixture(autouse=True)
def audit_stays_in_the_test(tmp_path, monkeypatch):
    """Keep every test's audit inside the test: a temporary outbox and no leftover state.

    ``worker.main()`` opens an audit run, so without this any test that reaches it wrote
    into the developer's own outbox. The module state is restored after each test, so a
    run left open by one cannot record into the next, and the log handler one test
    installs on the root logger cannot outlive it.
    """
    monkeypatch.setenv("AUDIT_OUTBOX_PATH", str(tmp_path / "outbox.sqlite"))
    monkeypatch.setattr(recorder, "_outbox", None)
    monkeypatch.setattr(recorder, "_run_id", None)
    monkeypatch.setattr(recorder, "_log_handler", None)
    yield
    # The handler is held by the root logger, which no fixture restores.
    recorder._remove_log_handler()


@pytest.fixture
def sample_lead():
    return {
        "lead": "Abogados García",
        "website": "https://abogadosgarcia.es",
        "phone": "965123456",
        "address": "Calle Mayor 1",
        "zip_code": "03201",
        "city": "Elche",
        "province": "Alicante",
        "maps_url": "https://maps.google.com/place/abogados-garcia",
    }


@pytest.fixture
def sample_analysis():
    return {
        "lead": "Abogados García",
        "website": "https://abogadosgarcia.es",
        "cms": "wordpress",
        "email": "info@abogadosgarcia.es",
        "instagram": "",
        "facebook": "https://facebook.com/abogadosgarcia",
        "youtube": "",
        "linkedin": "",
        "twitter": "",
        "tiktok": "",
        "seo_score": 60,
        "seo_issues": {"no_https": "Sin certificado SSL (web no segura)"},
    }


@pytest.fixture
def sample_job():
    return {
        "id": "lead-uuid-001",
        "business_name": "Abogados García",
        "website": "https://abogadosgarcia.es",
        "phone": "965123456",
        "email": "",
        "city": "Elche",
        "profession": "abogados",
        "lead_search_id": "search-uuid-001",
    }
