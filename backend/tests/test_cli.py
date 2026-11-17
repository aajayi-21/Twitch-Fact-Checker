"""The installable commands (app/cli.py) and where an install keeps its data
(app/config.py data_dir), so `uvx … doxa` works with nothing else set up."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app import cli
from app import config as config_module
from app.config import Settings, data_dir, running_from_checkout


class TestDataFolder:
    def test_a_checkout_keeps_its_data_in_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DOXA_DATA_DIR", raising=False)
        assert running_from_checkout() is True
        assert data_dir() == Path(config_module.__file__).resolve().parent.parent

    def test_an_installed_copy_uses_the_per_user_folder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Installed, app/ sits in site-packages: no pyproject.toml beside it."""
        monkeypatch.delenv("DOXA_DATA_DIR", raising=False)
        monkeypatch.setattr(config_module, "_BACKEND_DIR", tmp_path / "site-packages")
        monkeypatch.setattr(
            config_module.platformdirs,
            "user_data_dir",
            lambda name, appauthor: str(tmp_path / "user-data" / name),
        )
        assert running_from_checkout() is False
        assert data_dir() == tmp_path / "user-data" / "doxa"

    def test_doxa_data_dir_overrides_both(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DOXA_DATA_DIR", str(tmp_path / "custom"))
        assert data_dir() == tmp_path / "custom"


class TestSpeechEngineDefault:
    def test_with_the_engine_the_default_is_faster_whisper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STT_BACKEND", raising=False)
        monkeypatch.setattr(config_module, "speech_engine_installed", lambda: True)
        assert Settings(_env_file=None).stt_backend == "faster-whisper"

    def test_without_it_the_default_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STT_BACKEND", raising=False)
        monkeypatch.setattr(config_module, "speech_engine_installed", lambda: False)
        assert Settings(_env_file=None).stt_backend == "none"

    def test_choosing_an_engine_that_is_not_installed_fails_with_the_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config_module, "speech_engine_installed", lambda: False)
        with pytest.raises(ValidationError, match=r"doxa-backend\[stt\]"):
            Settings(_env_file=None, stt_backend="faster-whisper")


class TestCommands:
    @pytest.fixture()
    def ran(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        monkeypatch.setenv("DOXA_DATA_DIR", str(tmp_path / "data"))
        # The commands set HOST and PORT for the app's settings. Registering
        # them with monkeypatch first makes it restore them afterwards, so no
        # later test sees this test's address.
        for name in ("HOST", "PORT"):
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
        monkeypatch.setattr(
            cli.uvicorn,
            "run",
            lambda app, **kwargs: calls.append({"app": app, **kwargs}),
        )
        return calls

    def test_doxa_starts_the_viewer_backend_on_loopback(
        self, ran: list[dict[str, Any]], tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert cli.main([]) == 0
        assert ran == [
            {
                "app": "app.main:app",
                "host": "127.0.0.1",
                "port": 8710,
                "access_log": False,
            }
        ]
        assert (tmp_path / "data").is_dir(), "the data folder is created up front"
        assert str(tmp_path / "data") in capsys.readouterr().out

    def test_the_settings_learn_the_real_address(
        self, ran: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The startup banner and dashboard link read HOST/PORT from settings."""
        monkeypatch.delenv("HOST", raising=False)
        monkeypatch.delenv("PORT", raising=False)
        assert cli.main(["--port", "8741"]) == 0
        settings = Settings(_env_file=None)
        assert (settings.host, settings.port) == ("127.0.0.1", 8741)

    def test_doxa_streamer_starts_the_streamer_backend(
        self, ran: list[dict[str, Any]]
    ) -> None:
        assert cli.streamer_main(["--port", "9000"]) == 0
        assert ran[0]["app"] == "streamer.main:app"
        assert ran[0]["port"] == 9000

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.com", "::"])
    def test_a_non_loopback_address_is_refused(
        self, ran: list[dict[str, Any]], host: str
    ) -> None:
        assert cli.main(["--host", host]) == 2
        assert ran == []

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_addresses_are_accepted(
        self, ran: list[dict[str, Any]], host: str
    ) -> None:
        assert cli.main(["--host", host]) == 0
        assert ran[0]["host"] == host
