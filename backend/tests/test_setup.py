"""Key-only onboarding: unconfigured mode, /setup endpoints, hot-swap, upsert.

Everything runs offline: the provider validation probes are monkeypatched
(the real ones are zero-cost network calls), persistence goes to a TEMP
``.env`` via the ``ENV_FILE`` override, and the hot-swapped runtime is built
around the scriptable fakes from conftest. The user's real ``backend/.env``
is never read for key assertions nor written.
"""

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import setup as setup_api
from app.config import Settings, resolve_env_file
from app.llm_provider import LLMRuntime
from app.main import create_app
from app.models import TranscriptSegment
from app.rate_limit import QuotaCooldown
from app.setup import (
    CreditsInfo,
    ProviderKeyRejected,
    ProviderUnreachable,
    upsert_env_values,
)
from tests.conftest import (
    FakeGenAIClient,
    FakeTranscriber,
    make_fake_llm_runtime,
    make_gate_response,
    make_hello,
    make_test_settings,
    make_verdict_interaction,
    open_test_client,
    pcm_silence,
)

GEMINI_KEY = "gm-test-key-9xyz-abcd"
OPENROUTER_KEY = "sk-or-v1-test-key-wxyz"

CLAIM = "The Eiffel Tower is 450 meters tall."
SEVEN_WORD_SEGMENT = TranscriptSegment(
    # Seven words: below ClaimGate.MIN_NEW_WORDS, so the gate only runs on
    # the unconditional final flush pass — LLM-call counts stay deterministic.
    text="The Eiffel Tower is 450 meters tall",
    start=0.0,
    end=1.0,
    avg_logprob=-0.3,
    no_speech_prob=0.05,
)

BASE_ENV = (
    "# stream fact-checker backend config\n"
    "LLM_PROVIDER=openrouter\n"
    "OPENROUTER_API_KEY=sk-or-old-key-value\n"
    "\n"
    "# gemini section (key intentionally empty)\n"
    "GEMINI_API_KEY=\n"
    "WHISPER_MODEL=distil-small.en\n"
    # Dead port so the status payload's ollama reachability probe refuses
    # instantly and deterministically (offline-suite rule) even after
    # Settings is rebuilt from this file.
    "OLLAMA_BASE_URL=http://127.0.0.1:1/v1\n"
)

NOT_CONFIGURED_WS_MESSAGE = (
    "Backend has no API key yet — add one in the extension options."
)


@pytest.fixture()
def temp_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp ``.env`` installed as THE env file via the ENV_FILE override."""
    env_path = tmp_path / "setup-test.env"
    env_path.write_text(BASE_ENV, encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_path))
    return env_path


@pytest.fixture()
def unconfigured_client(
    temp_env_file: Path,
    fake_genai_client: FakeGenAIClient,
    fake_transcriber: FakeTranscriber,
) -> Iterator[TestClient]:
    """The real app booted UNCONFIGURED (no key for the active provider)."""
    settings = make_test_settings(gemini_api_key="", openrouter_api_key="")
    with open_test_client(settings, fake_genai_client, fake_transcriber) as client:
        yield client


def _accept_any_key_async(record: list[str]):  # type: ignore[no-untyped-def]
    """A fake async validation probe that records the submitted key."""

    async def probe(api_key: str) -> None:
        record.append(api_key)

    return probe


def _install_fake_runtime_builder(
    monkeypatch: pytest.MonkeyPatch, swap_client: FakeGenAIClient
) -> None:
    """Route the endpoint's post-validation rebuild through conftest fakes."""

    def fake_build(settings: Settings, cooldown: QuotaCooldown) -> LLMRuntime:
        return make_fake_llm_runtime(settings, swap_client, cooldown)

    monkeypatch.setattr(setup_api, "build_llm_runtime", fake_build)


def collect_frames_until_close(session: Any) -> tuple[list[dict[str, Any]], int]:
    """Drain server frames until the server closes; return (frames, close code)."""
    frames: list[dict[str, Any]] = []
    while True:
        try:
            frames.append(session.receive_json())
        except WebSocketDisconnect as disconnect:
            return frames, disconnect.code


# --------------------------------------------------------------------------- #
# Unconfigured surface: healthz, setup/status, debug, websocket
# --------------------------------------------------------------------------- #


class TestSetupCors:
    def test_preflight_allows_extension_origin_posting_json(
        self, unconfigured_client: TestClient
    ) -> None:
        """The options page POSTs JSON to /setup/credentials cross-origin."""
        origin = "chrome-extension://" + "a" * 32
        response = unconfigured_client.options(
            "/setup/credentials",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        allowed_headers = response.headers.get("access-control-allow-headers", "")
        assert "*" in allowed_headers or "content-type" in allowed_headers.lower()


class TestHostValidation:
    """DNS-rebinding hardening: only localhost Host headers are trusted.

    CORS cannot stop a rebound page (its requests are same-origin with an
    attacker-controlled Host), so TrustedHostMiddleware must reject any Host
    that is not 127.0.0.1/localhost — with or without a port suffix.
    """

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "127.0.0.1:8710", "localhost", "localhost:8710"],
    )
    def test_localhost_hosts_are_allowed(
        self, unconfigured_client: TestClient, host: str
    ) -> None:
        response = unconfigured_client.get("/healthz", headers={"host": host})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    @pytest.mark.parametrize(
        "host",
        [
            "rebound.attacker.example",
            "localhost.attacker.example",
            "192.168.1.50:8710",
        ],
    )
    def test_foreign_hosts_are_rejected_with_400(
        self, unconfigured_client: TestClient, host: str
    ) -> None:
        response = unconfigured_client.get("/healthz", headers={"host": host})
        assert response.status_code == 400
        assert "Invalid host header" in response.text

    def test_rebound_credentials_post_is_rejected_before_any_probe(
        self,
        unconfigured_client: TestClient,
        temp_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The DNS-rebinding key-replacement attack path must die at the
        Host check: no probe runs, nothing is persisted or swapped."""

        async def must_not_run(api_key: str) -> None:
            raise AssertionError("validation probe ran for a rejected Host")

        monkeypatch.setattr(setup_api, "validate_gemini_key", must_not_run)
        response = unconfigured_client.post(
            "/setup/credentials",
            headers={"host": "rebound.attacker.example"},
            json={"provider": "gemini", "api_key": "attacker-key"},
        )
        assert response.status_code == 400
        assert temp_env_file.read_text(encoding="utf-8") == BASE_ENV
        assert unconfigured_client.get("/healthz").json()["configured"] is False


class TestUnconfiguredSurface:
    def test_healthz_reports_unconfigured_with_null_llm_fields(
        self, unconfigured_client: TestClient
    ) -> None:
        response = unconfigured_client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "server_version": "0.1.0",
            "whisper_model": "fake-whisper.en",
            "configured": False,
            "llm_provider": None,
            "gate_provider": None,
            "verify_provider": None,
            "gate_model": None,
            "verify_model": None,
            "checks_today": 0,
            "est_cost_today_usd": 0.0,
        }

    def test_setup_status_reports_unconfigured_stages(
        self, unconfigured_client: TestClient
    ) -> None:
        response = unconfigured_client.get("/setup/status")
        assert response.status_code == 200
        assert response.json() == {
            "configured": False,
            "gate": {
                "provider": "gemini",
                "model": "fake-gate-model",
                "configured": False,
            },
            "verify": {
                "provider": "gemini",
                "model": "fake-verify-model",
                "configured": False,
            },
            "providers": {
                "openrouter": {
                    "configured": False,
                    "key_hint": None,
                    "credits": None,
                    "gate_model": "google/gemma-4-26b-a4b-it:free",
                    "verify_model": "google/gemma-4-26b-a4b-it:free",
                },
                "gemini": {"configured": False, "key_hint": None},
                "ollama": {
                    "configured": True,
                    "reachable": False,
                    "base_url": "http://127.0.0.1:1/v1",
                },
            },
        }

    def test_debug_text_is_409(self, unconfigured_client: TestClient) -> None:
        response = unconfigured_client.post(
            "/debug/text", json={"text": "The earth is flat."}
        )
        assert response.status_code == 409
        assert "not configured" in response.json()["detail"]

    def test_ws_hello_gets_not_configured_and_1011(
        self, unconfigured_client: TestClient
    ) -> None:
        with unconfigured_client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            error = session.receive_json()
            assert error == {
                "type": "error",
                "code": "not_configured",
                "message": NOT_CONFIGURED_WS_MESSAGE,
                "fatal": True,
            }
            with pytest.raises(WebSocketDisconnect) as disconnect:
                session.receive_json()
            assert disconnect.value.code == 1011


# --------------------------------------------------------------------------- #
# POST /setup/credentials: input validation and probe failures
# --------------------------------------------------------------------------- #


class TestCredentialsValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {"provider": "anthropic", "api_key": "sk-something"},
            {"provider": "", "api_key": "sk-something"},
            {"api_key": "sk-something"},  # provider missing entirely
        ],
    )
    def test_bad_provider_is_400(
        self, unconfigured_client: TestClient, body: dict[str, str]
    ) -> None:
        response = unconfigured_client.post("/setup/credentials", json=body)
        assert response.status_code == 400
        assert "provider" in response.json()["detail"]

    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_empty_key_is_400(
        self, unconfigured_client: TestClient, bad_key: str
    ) -> None:
        response = unconfigured_client.post(
            "/setup/credentials", json={"provider": "openrouter", "api_key": bad_key}
        )
        assert response.status_code == 400
        assert "api_key" in response.json()["detail"]

    def test_provider_rejection_is_401_and_nothing_persisted(
        self,
        unconfigured_client: TestClient,
        temp_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def reject(api_key: str) -> None:
            raise ProviderKeyRejected("User not found.")

        monkeypatch.setattr(setup_api, "validate_openrouter_key", reject)
        response = unconfigured_client.post(
            "/setup/credentials",
            json={"provider": "openrouter", "api_key": OPENROUTER_KEY},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "User not found."}
        # NOTHING persisted: the env file is byte-identical...
        assert temp_env_file.read_text(encoding="utf-8") == BASE_ENV
        # ...and the runtime was not swapped.
        assert unconfigured_client.get("/healthz").json()["configured"] is False

    def test_provider_unreachable_is_502(
        self,
        unconfigured_client: TestClient,
        temp_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def unreachable(api_key: str) -> None:
            raise ProviderUnreachable("could not reach Gemini: connect timeout")

        monkeypatch.setattr(setup_api, "validate_gemini_key", unreachable)
        response = unconfigured_client.post(
            "/setup/credentials", json={"provider": "gemini", "api_key": GEMINI_KEY}
        )
        assert response.status_code == 502
        assert "could not reach Gemini" in response.json()["detail"]
        assert temp_env_file.read_text(encoding="utf-8") == BASE_ENV


# --------------------------------------------------------------------------- #
# POST /setup/credentials: success path (fake probes) + hot-swap
# --------------------------------------------------------------------------- #


class TestCredentialsSuccess:
    def test_gemini_success_persists_swaps_and_serves_debug(
        self,
        unconfigured_client: TestClient,
        temp_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        probed_keys: list[str] = []
        monkeypatch.setattr(
            setup_api, "validate_gemini_key", _accept_any_key_async(probed_keys)
        )
        swap_client = FakeGenAIClient()
        _install_fake_runtime_builder(monkeypatch, swap_client)

        response = unconfigured_client.post(
            "/setup/credentials", json={"provider": "gemini", "api_key": GEMINI_KEY}
        )
        assert response.status_code == 200
        assert response.json() == {
            "configured": True,
            "gate": {
                "provider": "gemini",
                "model": "gemini-3.1-flash-lite",
                "configured": True,
            },
            "verify": {
                "provider": "gemini",
                "model": "gemini-3.5-flash",
                "configured": True,
            },
            "providers": {
                # The old OpenRouter key survives in the env file, so that
                # provider stays configured (its key line is untouched).
                "openrouter": {
                    "configured": True,
                    "key_hint": "…alue",
                    "credits": None,
                    "gate_model": "google/gemma-4-26b-a4b-it:free",
                    "verify_model": "google/gemma-4-26b-a4b-it:free",
                },
                "gemini": {"configured": True, "key_hint": "…abcd"},
                "ollama": {
                    "configured": True,
                    "reachable": False,
                    "base_url": "http://127.0.0.1:1/v1",
                },
            },
        }
        assert probed_keys == [GEMINI_KEY]

        # Upsert: ONLY the LLM_PROVIDER and GEMINI_API_KEY lines changed;
        # comments, blank lines, and the OTHER provider's key are preserved
        # byte-for-byte (including their positions).
        expected_env = BASE_ENV.replace(
            "LLM_PROVIDER=openrouter", "LLM_PROVIDER=gemini"
        ).replace("GEMINI_API_KEY=", f"GEMINI_API_KEY={GEMINI_KEY}")
        assert temp_env_file.read_text(encoding="utf-8") == expected_env

        # Hot-swap visible via /healthz without any restart...
        health = unconfigured_client.get("/healthz").json()
        assert health["configured"] is True
        assert health["llm_provider"] == "gemini"
        assert health["gate_model"] == "gemini-3.1-flash-lite"

        # ...and /debug/text now runs against the swapped-in (fake) stack.
        swap_client.generate_results.append(
            make_gate_response([("The Eiffel Tower is 450 meters tall.", 0.9)])
        )
        swap_client.interaction_results.append(
            make_verdict_interaction("FALSE", "It is about 330 meters tall.")
        )
        debug_response = unconfigured_client.post(
            "/debug/text", json={"text": "the eiffel tower is 450 meters tall"}
        )
        assert debug_response.status_code == 200
        verdicts = debug_response.json()["verdicts"]
        assert [v["label"] for v in verdicts] == ["FALSE"]

    def test_openrouter_success_reports_credits_and_touches_only_its_key(
        self,
        unconfigured_client: TestClient,
        temp_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        probed_keys: list[str] = []
        monkeypatch.setattr(
            setup_api,
            "validate_openrouter_key",
            _accept_any_key_async(probed_keys),
        )

        async def fake_credits(api_key: str) -> CreditsInfo:
            return CreditsInfo(total=10.0, usage=1.25)

        monkeypatch.setattr(setup_api, "fetch_openrouter_credits", fake_credits)
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())

        response = unconfigured_client.post(
            "/setup/credentials",
            json={"provider": "openrouter", "api_key": OPENROUTER_KEY},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is True
        assert body["gate"] == {
            "provider": "openrouter",
            "model": "google/gemma-4-26b-a4b-it:free",
            "configured": True,
        }
        assert body["verify"]["provider"] == "openrouter"
        openrouter_status = body["providers"]["openrouter"]
        assert openrouter_status["key_hint"] == "…wxyz"
        assert openrouter_status["credits"] == {"total": 10.0, "usage": 1.25}
        assert body["providers"]["gemini"] == {
            "configured": False,
            "key_hint": None,
        }
        assert probed_keys == [OPENROUTER_KEY]

        content = temp_env_file.read_text(encoding="utf-8")
        expected_env = BASE_ENV.replace(
            "OPENROUTER_API_KEY=sk-or-old-key-value",
            f"OPENROUTER_API_KEY={OPENROUTER_KEY}",
        )
        assert content == expected_env
        assert "GEMINI_API_KEY=\n" in content  # other provider untouched

        # /setup/status mirrors the POST response (openrouter -> credits).
        status = unconfigured_client.get("/setup/status").json()
        assert status["configured"] is True
        assert status["verify"]["provider"] == "openrouter"
        assert status["providers"]["openrouter"]["key_hint"] == "…wxyz"
        assert status["providers"]["openrouter"]["credits"] == {
            "total": 10.0,
            "usage": 1.25,
        }

    def test_key_never_appears_in_responses_or_logs(
        self,
        unconfigured_client: TestClient,
        temp_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        monkeypatch.setattr(setup_api, "validate_gemini_key", _accept_any_key_async([]))
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())

        post = unconfigured_client.post(
            "/setup/credentials", json={"provider": "gemini", "api_key": GEMINI_KEY}
        )
        status = unconfigured_client.get("/setup/status")
        health = unconfigured_client.get("/healthz")

        assert post.status_code == 200
        for response in (post, status, health):
            assert GEMINI_KEY not in response.text
        assert post.json()["providers"]["gemini"]["key_hint"] == "…abcd"
        for record in caplog.records:
            assert GEMINI_KEY not in record.getMessage()


# --------------------------------------------------------------------------- #
# Hot-swap vs. the live session and the process-wide cooldown
# --------------------------------------------------------------------------- #


class TestHotSwapPreemptsLiveSession:
    def test_live_session_gets_fatal_frame_and_next_session_uses_new_runtime(
        self,
        temp_env_file: Path,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: a session live across POST /setup/credentials holds
        the OLD client in its gate/checker; without a preempt it would keep
        'running' while every LLM call fails on a closed client. The swap
        must end the session with a fatal ``credentials_updated`` frame and
        a 1000 close, and the NEXT session must run on the new runtime."""
        monkeypatch.setattr(setup_api, "validate_gemini_key", _accept_any_key_async([]))
        swap_client = FakeGenAIClient()
        _install_fake_runtime_builder(monkeypatch, swap_client)
        settings = make_test_settings()  # configured boot (old fake client)

        with open_test_client(settings, fake_genai_client, fake_transcriber) as client:
            with client.websocket_connect("/ws/audio") as session:
                session.send_json(make_hello())
                assert session.receive_json()["type"] == "ready"

                response = client.post(
                    "/setup/credentials",
                    json={"provider": "gemini", "api_key": GEMINI_KEY},
                )
                assert response.status_code == 200

                fatal = session.receive_json()
                assert fatal["type"] == "error"
                assert fatal["code"] == "credentials_updated"
                assert fatal["fatal"] is True
                with pytest.raises(WebSocketDisconnect) as disconnect:
                    session.receive_json()
                assert disconnect.value.code == 1000

            # The next session's verdict is served by the swapped-in client;
            # the OLD client must see zero gate/verify calls end to end.
            fake_transcriber.segments_script.append([SEVEN_WORD_SEGMENT])
            swap_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
            swap_client.interaction_results.append(
                make_verdict_interaction("FALSE", "It is about 330 meters.")
            )
            with client.websocket_connect("/ws/audio") as second:
                second.send_json(make_hello())
                assert second.receive_json()["type"] == "ready"
                for _ in range(4):  # 1.0 s of audio in 250 ms frames
                    second.send_bytes(pcm_silence(0.25))
                second.send_json({"type": "stop"})
                frames, close_code = collect_frames_until_close(second)

            assert close_code == 1000
            verdict_labels = [
                frame["label"] for frame in frames if frame["type"] == "verdict"
            ]
            assert verdict_labels == ["FALSE"]
            assert fake_genai_client.generate_calls == []
            assert fake_genai_client.interaction_calls == []


class TestHotSwapInstallsFreshCooldown:
    def test_tripped_cooldown_is_not_carried_into_the_new_provider(
        self,
        unconfigured_client: TestClient,
        temp_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: the most likely reason for a mid-run key swap is a
        tripped cooldown (e.g. OpenRouter 402). The swap must install a
        FRESH QuotaCooldown atomically with the runtime — not reuse (or
        merely reset) the old instance — so new sessions and /debug/text
        verify immediately instead of replaying the old provider's reason."""
        monkeypatch.setattr(setup_api, "validate_gemini_key", _accept_any_key_async([]))
        swap_client = FakeGenAIClient()
        _install_fake_runtime_builder(monkeypatch, swap_client)

        old_cooldown: QuotaCooldown = unconfigured_client.app.state.quota_cooldown
        old_cooldown.trip(
            600.0, reason="OpenRouter credits exhausted — top up at openrouter.ai"
        )
        assert old_cooldown.active is True

        response = unconfigured_client.post(
            "/setup/credentials", json={"provider": "gemini", "api_key": GEMINI_KEY}
        )
        assert response.status_code == 200

        new_cooldown: QuotaCooldown = unconfigured_client.app.state.quota_cooldown
        assert new_cooldown is not old_cooldown
        assert new_cooldown.active is False

        # /debug/text reads app.state.quota_cooldown per request: the claim
        # must be verified now, not dropped with the stale OpenRouter reason.
        swap_client.generate_results.append(make_gate_response([(CLAIM, 0.9)]))
        swap_client.interaction_results.append(
            make_verdict_interaction("FALSE", "It is about 330 meters.")
        )
        debug_response = unconfigured_client.post(
            "/debug/text", json={"text": "the eiffel tower is 450 meters tall"}
        )
        assert debug_response.status_code == 200
        assert [v["label"] for v in debug_response.json()["verdicts"]] == ["FALSE"]


# --------------------------------------------------------------------------- #
# .env upsert (unit)
# --------------------------------------------------------------------------- #


class TestUpsertEnvValues:
    def test_replaces_values_and_preserves_everything_else(
        self, tmp_path: Path
    ) -> None:
        env_path = tmp_path / ".env"
        original = (
            "# leading comment\r\n"
            "LLM_PROVIDER=gemini\n"
            "  OPENROUTER_API_KEY = old-value\n"
            "\n"
            "# GEMINI_API_KEY=commented-out-stays\n"
            "GEMINI_API_KEY=old-gemini\r\n"
            "TRAILING=keep"  # no trailing newline
        )
        with env_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(original)
        upsert_env_values(
            env_path,
            {"LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "new-key"},
        )
        with env_path.open("r", encoding="utf-8", newline="") as handle:
            result = handle.read()
        assert result == (
            "# leading comment\r\n"
            "LLM_PROVIDER=openrouter\n"
            "OPENROUTER_API_KEY=new-key\n"
            "\n"
            "# GEMINI_API_KEY=commented-out-stays\n"
            "GEMINI_API_KEY=old-gemini\r\n"
            "TRAILING=keep"
        )

    def test_appends_missing_keys_after_final_newline_fixup(
        self, tmp_path: Path
    ) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("EXISTING=1", encoding="utf-8")  # no trailing \n
        upsert_env_values(env_path, {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "k"})
        assert env_path.read_text(encoding="utf-8") == (
            "EXISTING=1\nLLM_PROVIDER=gemini\nGEMINI_API_KEY=k\n"
        )

    def test_creates_file_and_parents_when_missing(self, tmp_path: Path) -> None:
        env_path = tmp_path / "nested" / "dir" / ".env"
        upsert_env_values(env_path, {"LLM_PROVIDER": "gemini"})
        assert env_path.read_text(encoding="utf-8") == "LLM_PROVIDER=gemini\n"
        assert (env_path.stat().st_mode & 0o777) == 0o600

    def test_tightens_permissive_mode_on_existing_file(self, tmp_path: Path) -> None:
        """Regression: a pre-existing world-readable .env (the common
        cp .env.example .env case, 0644) must come out 0600 after a key is
        written — the old mode must NOT be preserved onto the secret."""
        env_path = tmp_path / ".env"
        env_path.write_text("LLM_PROVIDER=openrouter\n", encoding="utf-8")
        os.chmod(env_path, 0o644)
        upsert_env_values(env_path, {"GEMINI_API_KEY": "gm-secret-value"})
        assert (env_path.stat().st_mode & 0o777) == 0o600
        assert "GEMINI_API_KEY=gm-secret-value" in env_path.read_text(encoding="utf-8")

    def test_rewrites_every_duplicate_occurrence(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(
            "GEMINI_API_KEY=first\nOTHER=x\nGEMINI_API_KEY=last\n", encoding="utf-8"
        )
        upsert_env_values(env_path, {"GEMINI_API_KEY": "new"})
        # python-dotenv honors the LAST occurrence, so both must be rewritten.
        assert env_path.read_text(encoding="utf-8") == (
            "GEMINI_API_KEY=new\nOTHER=x\nGEMINI_API_KEY=new\n"
        )

    def test_rejects_values_with_line_breaks(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        with pytest.raises(ValueError, match="multi-line"):
            upsert_env_values(env_path, {"GEMINI_API_KEY": "evil\nINJECTED=1"})
        assert not env_path.exists()


# --------------------------------------------------------------------------- #
# ENV_FILE override + Settings.is_configured
# --------------------------------------------------------------------------- #


class TestEnvFileOverride:
    def test_settings_read_the_overridden_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_path = tmp_path / "custom.env"
        env_path.write_text("WHISPER_MODEL=override-whisper\n", encoding="utf-8")
        monkeypatch.setenv("ENV_FILE", str(env_path))
        monkeypatch.delenv("WHISPER_MODEL", raising=False)
        assert resolve_env_file() == env_path
        assert Settings().whisper_model == "override-whisper"

    def test_missing_override_file_boots_unconfigured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("LLM_PROVIDER", "OPENROUTER_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("ENV_FILE", str(tmp_path / "does-not-exist.env"))
        settings = Settings()
        assert settings.is_configured is False

    def test_explicit_env_file_argument_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_path / "override.env"
        override.write_text("WHISPER_MODEL=from-override\n", encoding="utf-8")
        explicit = tmp_path / "explicit.env"
        explicit.write_text("WHISPER_MODEL=from-explicit\n", encoding="utf-8")
        monkeypatch.setenv("ENV_FILE", str(override))
        monkeypatch.delenv("WHISPER_MODEL", raising=False)
        assert Settings(_env_file=str(explicit)).whisper_model == "from-explicit"


class TestConfiguredBootFromEnvFile:
    def test_real_lifespan_builds_runtime_from_env_file_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Boot the REAL lifespan from a hand-written .env (via ENV_FILE):
        Settings -> is_configured -> build_llm_runtime composes the real
        Gemini gate/checker around the (faked) client, and /healthz plus the
        /ws/audio hello handshake report a configured backend. Only the two
        network edges are faked — the Whisper model load and the provider
        client constructor; the suite must stay offline."""
        env_path = tmp_path / "boot.env"
        env_path.write_text(
            "LLM_PROVIDER=gemini\nGEMINI_API_KEY=AIza-boot-test-key\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ENV_FILE", str(env_path))
        # Ambient environment variables override the env file in
        # pydantic-settings; clear any that could shadow the file's values.
        for variable in (
            "LLM_PROVIDER",
            "OPENROUTER_API_KEY",
            "GEMINI_API_KEY",
            "WHISPER_MODEL",
        ):
            monkeypatch.delenv(variable, raising=False)

        boot_client = FakeGenAIClient()

        def fake_transcriber_factory(*_args: Any, **_kwargs: Any) -> FakeTranscriber:
            return FakeTranscriber()

        def fake_create_llm_client(
            settings: Settings, provider: str
        ) -> FakeGenAIClient:
            assert provider == "gemini"
            assert settings.gemini_api_key == "AIza-boot-test-key"
            return boot_client

        monkeypatch.setattr("app.main.create_transcriber", fake_transcriber_factory)
        # Do NOT stub create_claim_gate/create_fact_checker: the point is
        # that the real build_llm_runtime composes the real Gemini stack.
        monkeypatch.setattr(
            "app.llm_provider.create_llm_client", fake_create_llm_client
        )

        with TestClient(
            create_app(),
            base_url="http://127.0.0.1",
            headers={"host": "127.0.0.1"},
        ) as boot_client_http:
            health = boot_client_http.get("/healthz").json()
            assert health["configured"] is True
            assert health["llm_provider"] == "gemini"
            assert health["gate_model"] == "gemini-3.1-flash-lite"
            assert health["verify_model"] == "gemini-3.5-flash"
            assert health["whisper_model"] == "distil-small.en"

            # A valid hello is answered with "ready" (no gate/verify call is
            # triggered; the unscripted fake would fail loudly if one were).
            with boot_client_http.websocket_connect("/ws/audio") as session:
                session.send_json(make_hello())
                ready = session.receive_json()
                assert ready["type"] == "ready"
                assert ready["model"] == "distil-small.en"


class TestIsConfigured:
    @pytest.mark.parametrize(
        ("provider", "kwargs", "expected"),
        [
            ("gemini", {"gemini_api_key": "AIza-real"}, True),
            ("gemini", {"gemini_api_key": ""}, False),
            ("gemini", {"gemini_api_key": "   "}, False),
            ("gemini", {"gemini_api_key": "# leftover comment"}, False),
            # Only the ACTIVE provider's key matters.
            ("gemini", {"gemini_api_key": "", "openrouter_api_key": "sk-or"}, False),
            ("openrouter", {"openrouter_api_key": "sk-or-real"}, True),
            ("openrouter", {"openrouter_api_key": ""}, False),
            ("openrouter", {"openrouter_api_key": "", "gemini_api_key": "k"}, False),
        ],
    )
    def test_active_provider_key_decides(
        self, provider: str, kwargs: dict[str, str], expected: bool
    ) -> None:
        settings = Settings(llm_provider=provider, _env_file=None, **kwargs)
        assert settings.is_configured is expected


# --------------------------------------------------------------------------- #
# Per-stage providers: ollama credentials probe + POST /setup/stages
# --------------------------------------------------------------------------- #

# Env file with BOTH keyed providers usable, so stage flips between them are
# legal and the post-swap Settings rebuild (which reads THIS file) stays
# configured. Dead ollama port per the offline-suite rule.
STAGES_ENV = (
    "LLM_PROVIDER=gemini\n"
    "GEMINI_API_KEY=AIza-stages-test-key\n"
    "OPENROUTER_API_KEY=sk-or-stages-test-key\n"
    "OLLAMA_BASE_URL=http://127.0.0.1:1/v1\n"
)


@pytest.fixture()
def stages_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    env_path = tmp_path / "stages-test.env"
    env_path.write_text(STAGES_ENV, encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_path))
    return env_path


def _probe_ollama_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(base_url: str) -> None:
        return None

    monkeypatch.setattr(setup_api, "probe_ollama", probe)


def _probe_ollama_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(base_url: str) -> None:
        raise ProviderUnreachable(f"no server at {base_url}")

    monkeypatch.setattr(setup_api, "probe_ollama", probe)


class TestOllamaCredentials:
    def test_reachable_probe_returns_status_and_persists_nothing(
        self,
        unconfigured_client: TestClient,
        temp_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _probe_ollama_ok(monkeypatch)
        response = unconfigured_client.post(
            "/setup/credentials", json={"provider": "ollama"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["providers"]["ollama"]["reachable"] is True
        # Pure test-connection: nothing persisted, nothing swapped.
        assert temp_env_file.read_text(encoding="utf-8") == BASE_ENV
        assert body["configured"] is False  # keyed stages still unconfigured

    def test_unreachable_probe_is_502_and_persists_nothing(
        self,
        unconfigured_client: TestClient,
        temp_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _probe_ollama_down(monkeypatch)
        response = unconfigured_client.post(
            "/setup/credentials", json={"provider": "ollama"}
        )
        assert response.status_code == 502
        assert "no server at" in response.json()["detail"]
        assert temp_env_file.read_text(encoding="utf-8") == BASE_ENV


class TestSetupStages:
    @pytest.fixture()
    def configured_client(
        self,
        stages_env_file: Path,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> Iterator[TestClient]:
        """App booted with BOTH keyed providers usable (mirrors STAGES_ENV)."""
        settings = make_test_settings(
            gemini_api_key="AIza-stages-test-key",
            openrouter_api_key="sk-or-stages-test-key",
        )
        with open_test_client(settings, fake_genai_client, fake_transcriber) as client:
            yield client

    def test_happy_path_persists_swaps_and_reports(
        self,
        configured_client: TestClient,
        stages_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())
        response = configured_client.post(
            "/setup/stages",
            json={"gate_provider": "openrouter", "verify_provider": "gemini"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is True
        assert body["gate"]["provider"] == "openrouter"
        assert body["verify"]["provider"] == "gemini"
        # Existing lines preserved byte-for-byte; the two stage keys appended.
        # No OPENROUTER_*_MODEL lines: this request submitted no slug, and
        # writing the code defaults would pin them into .env for a user who
        # never chose a model.
        assert stages_env_file.read_text(encoding="utf-8") == (
            STAGES_ENV + "GATE_PROVIDER=openrouter\n" + "VERIFY_PROVIDER=gemini\n"
        )
        # Hot-swap is visible on the very next status read (no restart).
        status = configured_client.get("/setup/status").json()
        assert status["gate"]["provider"] == "openrouter"
        assert status["verify"]["provider"] == "gemini"

    def test_ollama_gate_with_reachable_server(
        self,
        configured_client: TestClient,
        stages_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _probe_ollama_ok(monkeypatch)
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())
        response = configured_client.post(
            "/setup/stages",
            json={"gate_provider": "ollama", "verify_provider": "gemini"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["gate"] == {
            "provider": "ollama",
            "model": "gemma3:4b",
            "configured": True,
        }
        assert "GATE_PROVIDER=ollama" in stages_env_file.read_text(encoding="utf-8")

    def test_unknown_gate_provider_is_400(
        self, configured_client: TestClient, stages_env_file: Path
    ) -> None:
        response = configured_client.post(
            "/setup/stages",
            json={"gate_provider": "anthropic", "verify_provider": "gemini"},
        )
        assert response.status_code == 400
        assert stages_env_file.read_text(encoding="utf-8") == STAGES_ENV

    def test_ollama_verify_is_400_local_verify_unsupported(
        self, configured_client: TestClient, stages_env_file: Path
    ) -> None:
        response = configured_client.post(
            "/setup/stages",
            json={"gate_provider": "ollama", "verify_provider": "ollama"},
        )
        assert response.status_code == 400
        assert "local verify is not supported" in response.json()["detail"]
        assert stages_env_file.read_text(encoding="utf-8") == STAGES_ENV

    def test_unkeyed_stage_provider_is_409_naming_the_stage(
        self,
        stages_env_file: Path,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        # Booted with NO openrouter key: routing verify there must 409.
        settings = make_test_settings(
            gemini_api_key="AIza-stages-test-key", openrouter_api_key=""
        )
        with open_test_client(settings, fake_genai_client, fake_transcriber) as client:
            response = client.post(
                "/setup/stages",
                json={"gate_provider": "gemini", "verify_provider": "openrouter"},
            )
        assert response.status_code == 409
        assert "verify provider 'openrouter'" in response.json()["detail"]
        assert stages_env_file.read_text(encoding="utf-8") == STAGES_ENV

    def test_unreachable_ollama_gate_is_409_naming_the_stage(
        self,
        configured_client: TestClient,
        stages_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _probe_ollama_down(monkeypatch)
        response = configured_client.post(
            "/setup/stages",
            json={"gate_provider": "ollama", "verify_provider": "gemini"},
        )
        assert response.status_code == 409
        assert "gate provider 'ollama'" in response.json()["detail"]
        assert stages_env_file.read_text(encoding="utf-8") == STAGES_ENV


# --------------------------------------------------------------------------- #
# OpenRouter model slugs: live-catalogue validation + latch reset
# --------------------------------------------------------------------------- #

CATALOGUE = {
    "google/gemma-4-26b-a4b-it:free",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b:free",
}


def _catalogue_ok(monkeypatch: pytest.MonkeyPatch, calls: list[int]) -> None:
    async def fetch() -> set[str]:
        calls.append(1)
        return set(CATALOGUE)

    monkeypatch.setattr(setup_api, "fetch_openrouter_model_slugs", fetch)


def _catalogue_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch() -> set[str]:
        raise ProviderUnreachable("openrouter is down")

    monkeypatch.setattr(setup_api, "fetch_openrouter_model_slugs", fetch)


class TestOpenRouterModelSlugs:
    @pytest.fixture()
    def configured_client(
        self,
        stages_env_file: Path,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> Iterator[TestClient]:
        settings = make_test_settings(
            gemini_api_key="AIza-stages-test-key",
            openrouter_api_key="sk-or-stages-test-key",
        )
        with open_test_client(settings, fake_genai_client, fake_transcriber) as client:
            yield client

    def test_status_exposes_stored_slugs_even_when_stage_uses_another_provider(
        self, configured_client: TestClient
    ) -> None:
        """StageStatus.model shows the ACTIVE model; the options page needs
        the stored OpenRouter slugs to prefill regardless of routing."""
        body = configured_client.get("/setup/status").json()
        openrouter = body["providers"]["openrouter"]
        assert openrouter["gate_model"] == "google/gemma-4-26b-a4b-it:free"
        assert openrouter["verify_model"] == "google/gemma-4-26b-a4b-it:free"

    def test_valid_slugs_persist_and_hot_swap(
        self,
        configured_client: TestClient,
        stages_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[int] = []
        _catalogue_ok(monkeypatch, calls)
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())
        response = configured_client.post(
            "/setup/stages",
            json={
                "gate_provider": "openrouter",
                "verify_provider": "openrouter",
                "gate_model": "openai/gpt-oss-20b:free",
                "verify_model": "openai/gpt-oss-120b",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["gate"]["model"] == "openai/gpt-oss-20b:free"
        assert body["verify"]["model"] == "openai/gpt-oss-120b"
        assert body["providers"]["openrouter"]["gate_model"] == (
            "openai/gpt-oss-20b:free"
        )
        env = stages_env_file.read_text(encoding="utf-8")
        assert "OPENROUTER_GATE_MODEL=openai/gpt-oss-20b:free" in env
        assert "OPENROUTER_VERIFY_MODEL=openai/gpt-oss-120b" in env
        # One catalogue fetch covers both slugs.
        assert len(calls) == 1

    def test_unknown_slug_is_400_and_persists_nothing(
        self,
        configured_client: TestClient,
        stages_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _catalogue_ok(monkeypatch, [])
        before = stages_env_file.read_text(encoding="utf-8")
        response = configured_client.post(
            "/setup/stages",
            json={
                "gate_provider": "openrouter",
                "verify_provider": "openrouter",
                "gate_model": "acme/definitely-not-real",
                "verify_model": "",
            },
        )
        assert response.status_code == 400
        assert "no model" in response.json()["detail"]
        assert stages_env_file.read_text(encoding="utf-8") == before

    def test_free_suffix_typo_gets_a_did_you_mean(
        self, configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenRouter serves paid and :free as DISTINCT slugs — the most
        common mistake deserves a pointer, not just a rejection."""
        _catalogue_ok(monkeypatch, [])
        response = configured_client.post(
            "/setup/stages",
            json={
                "gate_provider": "openrouter",
                "verify_provider": "openrouter",
                "gate_model": "openai/gpt-oss-20b",
                "verify_model": "",
            },
        )
        assert response.status_code == 400
        assert "openai/gpt-oss-20b:free" in response.json()["detail"]

    def test_catalogue_unreachable_is_502(
        self, configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _catalogue_down(monkeypatch)
        response = configured_client.post(
            "/setup/stages",
            json={
                "gate_provider": "openrouter",
                "verify_provider": "openrouter",
                "gate_model": "openai/gpt-oss-120b",
                "verify_model": "",
            },
        )
        assert response.status_code == 502

    def test_omitted_slugs_skip_validation_entirely(
        self, configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Old clients send only the two provider fields; that must not cost
        a network round-trip nor change the stored slugs."""
        calls: list[int] = []
        _catalogue_ok(monkeypatch, calls)
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())
        response = configured_client.post(
            "/setup/stages",
            json={"gate_provider": "gemini", "verify_provider": "gemini"},
        )
        assert response.status_code == 200
        assert calls == []
        assert response.json()["providers"]["openrouter"]["gate_model"] == (
            "google/gemma-4-26b-a4b-it:free"
        )

    def test_model_change_resets_capability_latches(
        self,
        configured_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A latch learned from model A must never silently downgrade model B.

        The strict-json_schema latches are process-wide by design (they
        outlive per-session gate rebuilds), so a model swap has to clear them
        explicitly or the new model is stuck in json_object mode forever.
        """
        from app.llm_openrouter import OpenRouterClaimGate, _ReasoningSupport

        _catalogue_ok(monkeypatch, [])
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())
        OpenRouterClaimGate._json_schema_unsupported = True
        OpenRouterClaimGate._consecutive_strict_503s = 3
        OpenRouterClaimGate._json_schema_retry_at = 1e12
        _ReasoningSupport.unsupported = True

        response = configured_client.post(
            "/setup/stages",
            json={
                "gate_provider": "openrouter",
                "verify_provider": "openrouter",
                "gate_model": "openai/gpt-oss-120b",
                "verify_model": "",
            },
        )
        assert response.status_code == 200
        assert OpenRouterClaimGate._json_schema_unsupported is False
        assert OpenRouterClaimGate._consecutive_strict_503s == 0
        assert OpenRouterClaimGate._json_schema_retry_at == 0.0
        assert _ReasoningSupport.unsupported is False

    def test_unchanged_slug_leaves_latches_alone(
        self, configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-applying the SAME model must not throw away a hard-won latch."""
        from app.llm_openrouter import OpenRouterClaimGate

        _catalogue_ok(monkeypatch, [])
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())
        OpenRouterClaimGate._json_schema_unsupported = True
        response = configured_client.post(
            "/setup/stages",
            json={
                "gate_provider": "openrouter",
                "verify_provider": "openrouter",
                "gate_model": "google/gemma-4-26b-a4b-it:free",
                "verify_model": "google/gemma-4-26b-a4b-it:free",
            },
        )
        assert response.status_code == 200
        assert OpenRouterClaimGate._json_schema_unsupported is True


class TestFetchOpenRouterModelSlugs:
    """The catalogue probe itself, via the transport seam (no global patch)."""

    async def test_parses_ids(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == setup_api.OPENROUTER_MODELS_URL
            return httpx.Response(
                200,
                json={"data": [{"id": "a/b"}, {"id": "c/d:free"}, {"id": None}]},
            )

        slugs = await setup_api.fetch_openrouter_model_slugs(
            transport=httpx.MockTransport(handler)
        )
        assert slugs == {"a/b", "c/d:free"}

    @pytest.mark.parametrize(
        ("status_code", "payload", "text"),
        [
            (500, None, "boom"),
            (200, {"nope": 1}, None),
            (200, {"data": []}, None),
        ],
        ids=["http-500", "malformed", "empty"],
    )
    async def test_failures_are_unreachable(
        self, status_code: int, payload: Any, text: str | None
    ) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            if payload is None:
                return httpx.Response(status_code, text=text or "")
            return httpx.Response(status_code, json=payload)

        with pytest.raises(ProviderUnreachable):
            await setup_api.fetch_openrouter_model_slugs(
                transport=httpx.MockTransport(handler)
            )

    async def test_network_error_is_unreachable(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(ProviderUnreachable, match="could not reach"):
            await setup_api.fetch_openrouter_model_slugs(
                transport=httpx.MockTransport(handler)
            )


class TestStageSlugsOnlyValidateWhatChanged:
    """Validation keys on a slug CHANGING, not on it merely being present.

    The options page prefills these inputs with the stored slugs, so almost
    every Apply arrives with them populated. Gating the catalogue fetch on
    presence made a routing-only change depend on openrouter.ai being up, and
    let a stale slug in the untouched field reject a valid change to the
    other one.
    """

    @pytest.fixture()
    def configured_client(
        self,
        stages_env_file: Path,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> Iterator[TestClient]:
        settings = make_test_settings(
            gemini_api_key="AIza-stages-test-key",
            openrouter_api_key="sk-or-stages-test-key",
        )
        with open_test_client(settings, fake_genai_client, fake_transcriber) as client:
            yield client

    def test_resubmitting_the_stored_slug_skips_the_catalogue(
        self, configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prefilled, unchanged slug must not cost a network round-trip."""
        calls: list[int] = []
        _catalogue_ok(monkeypatch, calls)
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())
        stored = "google/gemma-4-26b-a4b-it:free"
        response = configured_client.post(
            "/setup/stages",
            json={
                "gate_provider": "openrouter",
                "verify_provider": "gemini",
                "gate_model": stored,
                "verify_model": stored,
            },
        )
        assert response.status_code == 200
        assert calls == []

    def test_routing_change_survives_an_unreachable_catalogue(
        self, configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Moving a stage to Ollama must not require openrouter.ai."""
        _catalogue_down(monkeypatch)
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())
        stored = "google/gemma-4-26b-a4b-it:free"
        response = configured_client.post(
            "/setup/stages",
            json={
                "gate_provider": "openrouter",
                "verify_provider": "gemini",
                "gate_model": stored,
                "verify_model": stored,
            },
        )
        assert response.status_code == 200

    def test_only_the_changed_slug_is_validated_and_persisted(
        self,
        configured_client: TestClient,
        stages_env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[int] = []
        _catalogue_ok(monkeypatch, calls)
        _install_fake_runtime_builder(monkeypatch, FakeGenAIClient())
        stored = "google/gemma-4-26b-a4b-it:free"
        response = configured_client.post(
            "/setup/stages",
            json={
                "gate_provider": "openrouter",
                "verify_provider": "openrouter",
                "gate_model": "openai/gpt-oss-20b:free",
                "verify_model": stored,
            },
        )
        assert response.status_code == 200
        env = stages_env_file.read_text(encoding="utf-8")
        assert "OPENROUTER_GATE_MODEL=openai/gpt-oss-20b:free" in env
        # The untouched field is not rewritten.
        assert "OPENROUTER_VERIFY_MODEL" not in env
        assert len(calls) == 1


class TestCatalogueParsing:
    async def test_non_dict_entry_is_a_502_not_a_500(self) -> None:
        """entry.get() on a string raises AttributeError; it must be caught.

        Uncaught, it escapes the ProviderUnreachable handler as an unhandled
        500 instead of the documented 502.
        """
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": ["not-a-dict"]})

        with pytest.raises(ProviderUnreachable, match="malformed"):
            await setup_api.fetch_openrouter_model_slugs(
                transport=httpx.MockTransport(handler)
            )
