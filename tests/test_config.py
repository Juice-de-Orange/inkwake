"""Tests for reading settings from the environment."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


def test_an_empty_variable_means_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker-compose.yml passes `${VAR:-}`, so "not set in .env" arrives as ""."""
    monkeypatch.setenv("USER_AGENT", "")
    monkeypatch.setenv("TZ_NAME", "  ")
    monkeypatch.setenv("EVENTS_COUNT", "")

    settings = Settings()

    assert settings.user_agent.startswith("inkwake/")
    assert settings.timezone.key == "Europe/Vienna"
    assert settings.events_count == 6


def test_ics_urls_are_a_comma_separated_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "EVENTS_ICS_URLS",
        " https://a.example.org/cal.ics ,, https://b.example.org/x.ics?key=abc ",
    )
    assert Settings().events_ics_urls == (
        "https://a.example.org/cal.ics",
        "https://b.example.org/x.ics?key=abc",
    )


def test_no_ics_urls_is_an_empty_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVENTS_ICS_URLS", raising=False)
    assert Settings().events_ics_urls == ()


def test_everything_the_service_writes_lives_under_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One volume to mount and one to back up."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings()
    for path in (settings.db_path, settings.firmware_dir, settings.image_dir):
        assert path.is_relative_to(tmp_path), path


def test_public_base_url_loses_its_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://eink.example.com/")
    assert Settings().public_base_url == "https://eink.example.com"


def test_a_malformed_number_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENTS_COUNT", "six")
    with pytest.raises(ValueError, match="EVENTS_COUNT"):
        Settings()
