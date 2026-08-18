"""Tests for the streamer app: the product split, /ws/events, bot routes.

The core assertion of this module is the SPLIT itself: the streamer app is a
superset that serves its own surfaces on its own state, while the viewer app
(`app.main`) carries none of them — two products, two databases, zero
cross-talk.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.db import DayCounter
from app.events import EventHub
from app.rate_limit import QuotaCooldown, TokenBucket
from app.sessions import SessionRegistry

from streamer.config import StreamerSettings
from streamer.db import StreamerDatabase
from streamer.main import create_app as create_streamer_app
from tests.conftest import (
    FakeGenAIClient,
    FakeTranscriber,
    make_fake_llm_runtime,
    make_test_settings,
)

from concurrent.futures import ThreadPoolExecutor


def make_streamer_settings(tmp_path: Path, **overrides: Any) -> StreamerSettings:
    viewer_defaults = make_test_settings().model_dump()
    viewer_defaults.update(
        {
            "db_path": str(tmp_path / "streamer-app-test.db"),
            "port": 8711,
        }
    )
    viewer_defaults.update(overrides)
    return StreamerSettings(_env_file=None, **viewer_defaults)


def install_fake_streamer_state(
    application: FastAPI, settings: StreamerSettings
) -> None:
    """The streamer twin of conftest's _install_fake_state: real routers and
    hub, fakes for everything heavy, and NO live chat bot."""

    @asynccontextmanager
    async def fake_lifespan(app: FastAPI):
        cooldown = QuotaCooldown()
        app.state.settings = settings
        app.state.quota_cooldown = cooldown
        app.state.verify_bucket = TokenBucket(
            rate_per_min=settings.verify_rpm, burst=10
        )
        app.state.llm_runtime = make_fake_llm_runtime(
            settings, FakeGenAIClient(), cooldown
        )
        app.state.sessions = SessionRegistry(
            scope=settings.session_preempt_scope,
            max_sessions=settings.max_sessions,
        )
        app.state.events = EventHub(settings.event_queue_maxsize)
        db = StreamerDatabase(settings.db_path)
        await db.open()
        app.state.db = db
        app.state.verify_counter = DayCounter(initial=0)
        app.state.transcriber = FakeTranscriber()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-test")
        app.state.stt_executor = executor
        app.state.chat_bot = None
        app.state.chat_bot_task = None
        app.state.ensure_chat_bot = lambda: None
        try:
            yield
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            await db.close()

    application.router.lifespan_context = fake_lifespan


@pytest.fixture()
def streamer_client(tmp_path: Path) -> Iterator[TestClient]:
    application = create_streamer_app()
    install_fake_streamer_state(application, make_streamer_settings(tmp_path))
    with TestClient(
        application, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}
    ) as test_client:
        yield test_client


class TestProductSplit:
    def test_streamer_surfaces_do_not_exist_on_the_viewer_app(
        self, client: TestClient
    ) -> None:
        """`client` is the VIEWER app fixture from conftest. The streamer's
        surfaces must not leak into it — the split is real, not cosmetic."""
        for path in ("/meta/topics", "/bot/status", "/setup/twitch", "/stats/chat"):
            assert client.get(path).status_code == 404, path
        assert client.post("/events/test", json={}).status_code == 404

    def test_streamer_app_serves_the_shared_surface_too(
        self, streamer_client: TestClient
    ) -> None:
        """The streamer app is a SUPERSET: healthz, stats, setup all work."""
        health = streamer_client.get("/healthz").json()
        assert health["product"] == "streamer"
        assert health["twitch_configured"] is False
        assert streamer_client.get("/stats/summary").status_code == 200
        assert streamer_client.get("/setup/status").status_code == 200

    def test_meta_topics_serves_the_canonical_palette(
        self, streamer_client: TestClient
    ) -> None:
        payload = streamer_client.get("/meta/topics").json()
        by_slug = {topic["slug"]: topic for topic in payload["topics"]}
        assert by_slug["politics"]["color"] == "#e74c3c"
        assert by_slug["other"]["label"] == "Everything else"
        assert len(payload["topics"]) == 9


class TestEventsSocket:
    def test_subscribes_and_receives_a_test_event(
        self, streamer_client: TestClient
    ) -> None:
        with streamer_client.websocket_connect("/ws/events") as socket:
            assert socket.receive_json()["type"] == "events_ready"
            response = streamer_client.post(
                "/events/test", json={"label": "MISLEADING"}
            )
            assert response.status_code == 200
            event = socket.receive_json()
            assert event["type"] == "event"
            assert event["channel"] == "__test__"
            assert event["frame"]["label"] == "MISLEADING"
            assert "test verdict" in event["frame"]["claim"]

    def test_type_filter_is_honored(self, streamer_client: TestClient) -> None:
        with streamer_client.websocket_connect(
            "/ws/events?types=contradiction"
        ) as socket:
            assert socket.receive_json()["type"] == "events_ready"
            streamer_client.post("/events/test", json={})
            hub: EventHub = streamer_client.app.state.events
            # The verdict was filtered at subscription time, not delivered.
            assert all(
                subscription._queue.empty()
                for subscription in hub._subscribers.values()
            )

    def test_foreign_origin_is_rejected(self, streamer_client: TestClient) -> None:
        """CORS does not cover WebSocket handshakes; the explicit Origin
        check is what stops any web page from subscribing to the stream."""
        with pytest.raises(WebSocketDisconnect):
            with streamer_client.websocket_connect(
                "/ws/events", headers={"origin": "https://evil.example"}
            ):
                pass

    def test_local_origin_is_accepted(self, streamer_client: TestClient) -> None:
        with streamer_client.websocket_connect(
            "/ws/events", headers={"origin": "http://127.0.0.1:8711"}
        ) as socket:
            assert socket.receive_json()["type"] == "events_ready"

    def test_events_token_gates_when_configured(self, tmp_path: Path) -> None:
        application = create_streamer_app()
        install_fake_streamer_state(
            application,
            make_streamer_settings(tmp_path, events_token="sekrit"),
        )
        with TestClient(
            application, base_url="http://127.0.0.1", headers={"host": "127.0.0.1"}
        ) as gated:
            with pytest.raises(WebSocketDisconnect):
                with gated.websocket_connect("/ws/events"):
                    pass
            with gated.websocket_connect("/ws/events?token=sekrit") as socket:
                assert socket.receive_json()["type"] == "events_ready"


class TestBotRoutes:
    def test_status_without_a_bot_is_honest(self, streamer_client: TestClient) -> None:
        status = streamer_client.get("/bot/status").json()
        assert status["configured"] is False
        assert status["bot"] is None
        assert status["dry_run"] is True  # the default posture

    def test_mutations_without_a_bot_are_409(self, streamer_client: TestClient) -> None:
        assert (
            streamer_client.post("/bot/mode", json={"mode": "off"}).status_code == 409
        )
        assert (
            streamer_client.post("/bot/mute", json={"muted": True}).status_code == 409
        )
        assert (
            streamer_client.post("/bot/dry-run", json={"dry_run": False}).status_code
            == 409
        )

    def test_chat_stats_endpoint_serves_the_histogram(
        self, streamer_client: TestClient
    ) -> None:
        summary = streamer_client.get("/stats/chat").json()
        assert summary == {"by_status": {}, "drop_reasons": {}}


class TestTwitchSetupRoutes:
    def test_status_reports_unconfigured(self, streamer_client: TestClient) -> None:
        status = streamer_client.get("/setup/twitch").json()
        assert status["configured"] is False
        assert status["token_hint"] is None
        assert status["client_id_set"] is False

    def test_device_flow_without_a_client_id_is_409_with_guidance(
        self, streamer_client: TestClient
    ) -> None:
        response = streamer_client.post("/setup/twitch/device")
        assert response.status_code == 409
        assert "dev.twitch.tv" in response.json()["detail"]

    def test_device_poll_without_a_flow_is_409(
        self, streamer_client: TestClient
    ) -> None:
        assert (
            streamer_client.post("/setup/twitch/device/poll", json={}).status_code
            == 409
        )
