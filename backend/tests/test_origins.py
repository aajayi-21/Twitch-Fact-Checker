"""Which browser origins may reach the local backend (app/origins.py).

Every page the user has open can try 127.0.0.1. These tests pin down that
only Doxa's extension, the backend's own pages and non-browser clients get
through, over HTTP and over both WebSockets.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.origins import (
    DEFAULT_ALLOWED_EXTENSION_IDS,
    DEV_EXTENSION_ID,
    origin_allowed,
    parse_extension_ids,
)
from tests.conftest import (
    FakeLLMClient,
    FakeTranscriber,
    make_hello,
    make_test_settings,
    open_test_client,
)

DEV_ORIGIN = f"chrome-extension://{DEV_EXTENSION_ID}"
OTHER_EXTENSION_ID = "b" * 32
OTHER_EXTENSION_ORIGIN = f"chrome-extension://{OTHER_EXTENSION_ID}"
WEBSITE = "https://evil.example"
IDS = frozenset({DEV_EXTENSION_ID})


def allowed(origin: str | None, *, host: str = "127.0.0.1:8710", path: str = "/x"):
    return origin_allowed(origin, host=host, path=path, extension_ids=IDS)


class TestOriginRule:
    def test_no_origin_is_a_local_non_browser_client(self) -> None:
        assert allowed(None)

    def test_the_listed_extension_passes(self) -> None:
        assert allowed(DEV_ORIGIN)
        assert allowed(DEV_ORIGIN.upper())

    def test_any_other_extension_is_refused(self) -> None:
        assert not allowed(OTHER_EXTENSION_ORIGIN)

    def test_the_backends_own_pages_pass(self) -> None:
        assert allowed("http://127.0.0.1:8710", host="127.0.0.1:8710")
        assert allowed("http://localhost:8711", host="localhost:8711")

    def test_another_local_port_or_name_is_refused(self) -> None:
        assert not allowed("http://127.0.0.1:3000", host="127.0.0.1:8710")
        assert not allowed("http://localhost:8710", host="127.0.0.1:8710")

    def test_websites_are_refused(self) -> None:
        assert not allowed(WEBSITE)
        assert not allowed("http://127.0.0.1.evil.example", host="127.0.0.1")

    def test_null_origin_only_on_read_only_paths(self) -> None:
        assert not allowed("null", path="/ws/audio")
        assert not allowed("null", path="/setup/credentials")
        assert allowed("null", path="/ws/events")

    def test_no_host_header_means_no_same_origin_match(self) -> None:
        assert not origin_allowed(
            "http://127.0.0.1", host=None, path="/x", extension_ids=IDS
        )


class TestExtensionIdSetting:
    def test_parses_a_comma_separated_list(self) -> None:
        raw = f" {DEV_EXTENSION_ID} , {OTHER_EXTENSION_ID.upper()},,"
        assert parse_extension_ids(raw) == {DEV_EXTENSION_ID, OTHER_EXTENSION_ID}

    def test_a_typo_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="not a Chrome extension ID"):
            parse_extension_ids(f"{DEV_EXTENSION_ID},abc")

    def test_settings_default_to_the_development_extension(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.allowed_extension_id_set == set(DEFAULT_ALLOWED_EXTENSION_IDS)
        assert DEV_EXTENSION_ID in settings.allowed_extension_id_set

    def test_settings_refuse_an_invalid_id_at_startup(self) -> None:
        with pytest.raises(ValidationError, match="ALLOWED_EXTENSION_IDS"):
            Settings(_env_file=None, allowed_extension_ids="not-an-id")

    def test_debug_endpoints_are_off_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEBUG_ENDPOINTS", raising=False)
        assert Settings(_env_file=None).debug_endpoints is False


class TestViewerBackendHttp:
    def test_requests_without_an_origin_pass(self, client: TestClient) -> None:
        assert client.get("/healthz").status_code == 200

    def test_the_extension_passes(self, client: TestClient) -> None:
        response = client.get("/healthz", headers={"origin": DEV_ORIGIN})
        assert response.status_code == 200

    def test_the_backends_own_page_passes(self, client: TestClient) -> None:
        response = client.get("/healthz", headers={"origin": "http://127.0.0.1"})
        assert response.status_code == 200

    @pytest.mark.parametrize(
        "origin", [WEBSITE, OTHER_EXTENSION_ORIGIN, "http://127.0.0.1:3000", "null"]
    )
    def test_everything_else_is_refused(self, client: TestClient, origin: str) -> None:
        response = client.get("/healthz", headers={"origin": origin})
        assert response.status_code == 403
        assert response.json()["detail"]

    def test_an_unlisted_extension_is_told_how_to_get_in(
        self, client: TestClient
    ) -> None:
        response = client.get("/healthz", headers={"origin": OTHER_EXTENSION_ORIGIN})
        detail = response.json()["detail"]
        assert OTHER_EXTENSION_ID in detail and "ALLOWED_EXTENSION_IDS" in detail

    def test_a_simple_cross_site_post_never_reaches_the_route(
        self, client: TestClient
    ) -> None:
        """A form-style POST needs no preflight, so CORS alone would let it
        run. The guard refuses it before the body is even parsed."""
        response = client.post(
            "/setup/credentials",
            content="provider=anthropic&api_key=",
            headers={
                "origin": WEBSITE,
                "content-type": "application/x-www-form-urlencoded",
            },
        )
        assert response.status_code == 403


class TestViewerBackendAudioSocket:
    @pytest.mark.parametrize("origin", [WEBSITE, OTHER_EXTENSION_ORIGIN, "null"])
    def test_refused_origins_get_no_socket(
        self, client: TestClient, origin: str
    ) -> None:
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/ws/audio", headers={"origin": origin}):
                pass
        assert refused.value.code == 1008

    def test_the_extension_gets_a_session(self, client: TestClient) -> None:
        with client.websocket_connect(
            "/ws/audio", headers={"origin": DEV_ORIGIN}
        ) as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"

    def test_a_website_cannot_end_the_live_session(self, client: TestClient) -> None:
        with client.websocket_connect(
            "/ws/audio", headers={"origin": DEV_ORIGIN}
        ) as live:
            live.send_json(make_hello())
            assert live.receive_json()["type"] == "ready"
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws/audio", headers={"origin": WEBSITE}):
                    pass
            live.send_json({"type": "stop"})
            frames = []
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    frames.append(live.receive_json())
        assert closed.value.code == 1000
        assert not [frame for frame in frames if frame.get("code") == "superseded"]


class TestAllowListFollowsSettings:
    @pytest.fixture()
    def custom_client(
        self, fake_llm_client: FakeLLMClient, fake_transcriber: FakeTranscriber
    ) -> Iterator[TestClient]:
        settings = make_test_settings(allowed_extension_ids=OTHER_EXTENSION_ID)
        with open_test_client(settings, fake_llm_client, fake_transcriber) as client:
            yield client

    def test_only_the_configured_ids_pass(self, custom_client: TestClient) -> None:
        listed = custom_client.get(
            "/healthz", headers={"origin": OTHER_EXTENSION_ORIGIN}
        )
        unlisted = custom_client.get("/healthz", headers={"origin": DEV_ORIGIN})
        assert listed.status_code == 200
        assert unlisted.status_code == 403


class TestStreamerBackend:
    @pytest.fixture()
    def streamer_client(self, tmp_path: Path) -> Iterator[TestClient]:
        from streamer.main import create_app as create_streamer_app
        from tests.test_streamer_app import (
            install_fake_streamer_state,
            make_streamer_settings,
        )

        application = create_streamer_app()
        install_fake_streamer_state(application, make_streamer_settings(tmp_path))
        with TestClient(
            application, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}
        ) as test_client:
            yield test_client

    def test_an_obs_source_with_a_null_origin_can_read_events(
        self, streamer_client: TestClient
    ) -> None:
        with streamer_client.websocket_connect(
            "/ws/events", headers={"origin": "null"}
        ) as socket:
            assert socket.receive_json()["type"] == "events_ready"

    def test_a_null_origin_cannot_control_the_bot(
        self, streamer_client: TestClient
    ) -> None:
        response = streamer_client.post(
            "/bot/mute", json={"muted": True}, headers={"origin": "null"}
        )
        assert response.status_code == 403

    def test_a_website_cannot_restart_twitch_pairing(
        self, streamer_client: TestClient
    ) -> None:
        """This route takes no body, so it needs no preflight: only the guard
        stops a website from firing it."""
        response = streamer_client.post(
            "/setup/twitch/device", headers={"origin": WEBSITE}
        )
        assert response.status_code == 403

    def test_a_website_cannot_open_the_audio_socket(
        self, streamer_client: TestClient
    ) -> None:
        with pytest.raises(WebSocketDisconnect):
            with streamer_client.websocket_connect(
                "/ws/audio", headers={"origin": WEBSITE}
            ):
                pass
