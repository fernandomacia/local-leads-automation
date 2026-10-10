"""Tests for config.py — the guards that decide whether an environment is usable.

Run in a subprocess, because config.py does its checking at import time and is
already imported by the time any test runs. The environment is built from scratch
rather than inherited: a key the parent exported would satisfy a guard that the env
file itself does not, which is the very mistake these tests are about.
"""

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_COMPLETE = {
    "API_BASE_URL": "http://localhost:8000",
    "API_TOKEN": "token",
    "SENDER_COMPANY": "SegurSEO",
    "OPENROUTER_API_KEY": "sk-test",
}


def _write_env(path: Path, **overrides) -> Path:
    values = {**_COMPLETE, **overrides}
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None))
    return path


def _import_config(env_file) -> subprocess.CompletedProcess:
    """Import config.py in a clean process pointed at env_file."""
    return subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=_ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "WORKER_ENV_FILE": str(env_file)},
    )


class TestEnvironmentGuards:
    def test_a_complete_file_imports(self, tmp_path):
        result = _import_config(_write_env(tmp_path / "ok.env"))
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("missing", ["API_BASE_URL", "API_TOKEN"])
    def test_the_api_credentials_are_required(self, tmp_path, missing):
        result = _import_config(_write_env(tmp_path / "x.env", **{missing: None}))
        assert result.returncode != 0
        assert "API_BASE_URL and API_TOKEN" in result.stderr

    @pytest.mark.parametrize("missing", ["OPENROUTER_API_KEY", "SENDER_COMPANY"])
    def test_the_pitch_credentials_are_required(self, tmp_path, missing):
        # Not a convenience: OpenRouter answers a missing key with 401, which is not the
        # 402 the worker treats as a funding pause, so every lead of the search is
        # reported failed and retired for good.
        result = _import_config(_write_env(tmp_path / "x.env", **{missing: None}))
        assert result.returncode != 0
        assert "OPENROUTER_API_KEY and SENDER_COMPANY" in result.stderr
        assert "x.env" in result.stderr, "the error must name the file that is short a line"

    def test_a_named_file_that_does_not_exist_says_so(self, tmp_path):
        result = _import_config(tmp_path / "nope.env")
        assert result.returncode != 0
        assert "which is not a file" in result.stderr
        assert "must be set" not in result.stderr, "pointing at the wrong cause"

    def test_an_absolute_path_is_taken_as_given(self, tmp_path):
        # What lets a service unit keep its env file outside the checkout.
        env_file = _write_env(tmp_path / "outside.env")
        assert env_file.is_absolute() and _ROOT not in env_file.parents
        assert _import_config(env_file).returncode == 0

    def test_plain_http_is_refused_for_a_remote_host(self, tmp_path):
        # The bearer token travels on every request; the mistake this catches is
        # copying the local file and only changing the host.
        result = _import_config(_write_env(tmp_path / "x.env", API_BASE_URL="http://api.example.com"))
        assert result.returncode != 0
        assert "https://" in result.stderr
