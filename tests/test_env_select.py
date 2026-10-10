"""Tests for env_select.py — the startup choice of API, without a real terminal."""

import io
import os

import pytest

import env_select


class _Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def unchosen(monkeypatch, tmp_path):
    # setenv before delenv so teardown removes whatever choose_env_file sets.
    monkeypatch.setenv("WORKER_ENV_FILE", "")
    monkeypatch.delenv("WORKER_ENV_FILE")
    monkeypatch.setattr(env_select, "_ROOT", tmp_path)
    (tmp_path / ".env").write_text("API_BASE_URL=http://localhost:8000\n")
    (tmp_path / ".env.production").write_text("API_BASE_URL=https://platform.segurseo.com\n")
    monkeypatch.setattr(env_select.sys, "stdin", _Tty())
    return tmp_path


def test_an_explicit_file_skips_the_question(monkeypatch):
    monkeypatch.setenv("WORKER_ENV_FILE", ".env.production")
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("asked anyway"))
    env_select.choose_env_file()
    assert os.environ["WORKER_ENV_FILE"] == ".env.production"


def test_without_a_terminal_it_refuses_instead_of_guessing(unchosen, monkeypatch):
    monkeypatch.setattr(env_select.sys, "stdin", io.StringIO())
    with pytest.raises(SystemExit):
        env_select.choose_env_file()


def test_enter_picks_local(unchosen, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    env_select.choose_env_file()
    assert os.environ["WORKER_ENV_FILE"] == ".env"


def test_an_empty_value_counts_as_unset(unchosen, monkeypatch):
    # An exported WORKER_ENV_FILE=$UNSET_VAR used to skip the question, leaving
    # config.py resolving the repo directory itself as the env file.
    monkeypatch.setenv("WORKER_ENV_FILE", "")
    monkeypatch.setattr("builtins.input", lambda _: "2")
    env_select.choose_env_file()
    assert os.environ["WORKER_ENV_FILE"] == ".env.production"


def test_an_invalid_answer_is_asked_again(unchosen, monkeypatch, capsys):
    answers = iter(["7", "nope", "2"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    env_select.choose_env_file()
    assert os.environ["WORKER_ENV_FILE"] == ".env.production"
    assert capsys.readouterr().out.count("Invalid option.") == 2


def test_with_no_env_file_at_all_it_says_what_to_copy(unchosen, monkeypatch):
    for name in (".env", ".env.production"):
        (unchosen / name).unlink()
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("asked with nothing to offer"))
    with pytest.raises(SystemExit, match=".env.example"):
        env_select.choose_env_file()


def test_only_one_file_present_becomes_the_default(unchosen, monkeypatch):
    (unchosen / ".env").unlink()
    monkeypatch.setattr("builtins.input", lambda _: "")
    env_select.choose_env_file()
    assert os.environ["WORKER_ENV_FILE"] == ".env.production"


def test_production_is_chosen_explicitly(unchosen, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: "2")
    env_select.choose_env_file()
    assert os.environ["WORKER_ENV_FILE"] == ".env.production"
    # The URL comes from the file, so a mis-filled production file shows before start.
    assert "https://platform.segurseo.com" in capsys.readouterr().out
