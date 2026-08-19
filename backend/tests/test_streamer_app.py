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


def install_bot(client: TestClient):
    """Attach a real ChannelBot (fake transport) to the running test app."""
    from streamer.chat.bot import ChannelBot
    from streamer.chat.policy import PostingPolicy
    from tests.test_chat_bot import CHANNEL, FakeChatTransport

    state = client.app.state
    bot = ChannelBot(
        platform="twitch",
        channel=CHANNEL,
        transport=FakeChatTransport(),  # type: ignore[arg-type]
        db=state.db,
        hub=state.events,
        registry=state.sessions,
        allowlist=frozenset({CHANNEL}),
        policy=PostingPolicy(mode="auto"),
        dry_run=True,
    )
    state.chat_bot = bot
    return bot


class TestSettingsRoutes:
    def test_get_settings_serves_the_full_policy(
        self, streamer_client: TestClient
    ) -> None:
        install_bot(streamer_client)
        config = streamer_client.get("/bot/settings").json()
        # Every knob the panel renders comes from this one payload.
        for key in (
            "mode",
            "labels",
            "topics",
            "min_check_worthiness",
            "min_sources",
            "posts_per_hour",
            "min_gap_s",
            "claim_cooldown_s",
            "topic_cooldown_s",
            "max_claim_age_s",
            "template",
            "sources_style",
            "source_tiers_extra",
        ):
            assert key in config, key

    def test_partial_update_applies_and_reads_back(
        self, streamer_client: TestClient
    ) -> None:
        bot = install_bot(streamer_client)
        response = streamer_client.post(
            "/bot/settings",
            json={"posts_per_hour": 3, "template": "compact"},
        )
        assert response.status_code == 200
        assert response.json()["bot"]["settings"]["posts_per_hour"] == 3
        assert bot.policy.template == "compact"
        assert bot.policy.mode == "auto"  # untouched

    def test_clamp_violation_is_a_400_with_the_reason(
        self, streamer_client: TestClient
    ) -> None:
        """Refused loudly, never clamped silently — a form showing a value
        the backend quietly rewrote would be lying."""
        bot = install_bot(streamer_client)
        response = streamer_client.post("/bot/settings", json={"posts_per_hour": 50})
        assert response.status_code == 400
        assert "posts_per_hour must be 1..12" in response.json()["detail"]
        assert bot.policy.posts_per_hour == 6  # unchanged

    def test_unknown_key_is_a_400(self, streamer_client: TestClient) -> None:
        install_bot(streamer_client)
        response = streamer_client.post("/bot/settings", json={"nonsense": 1})
        assert response.status_code == 400
        assert "unknown settings" in response.json()["detail"]

    def test_unverified_stays_unconfigurable_via_the_api(
        self, streamer_client: TestClient
    ) -> None:
        install_bot(streamer_client)
        response = streamer_client.post(
            "/bot/settings", json={"labels": ["FALSE", "UNVERIFIED"]}
        )
        assert response.status_code == 400
        assert "UNVERIFIED" in response.json()["detail"]

    def test_trust_toggles_probation_in_the_status(
        self, streamer_client: TestClient
    ) -> None:
        install_bot(streamer_client)
        status = streamer_client.post("/bot/trust", json={"trusted": True}).json()
        assert status["bot"]["probation"]["active"] is False
        status = streamer_client.post("/bot/trust", json={"trusted": False}).json()
        assert status["bot"]["probation"]["active"] is True

    def test_retract_validates_the_handle(self, streamer_client: TestClient) -> None:
        install_bot(streamer_client)
        assert (
            streamer_client.post(
                "/bot/retract", json={"handle": "/ban all"}
            ).status_code
            == 400
        )
        assert (
            streamer_client.post("/bot/retract", json={"handle": "abcd12"}).status_code
            == 404  # valid shape, no such post
        )

    def test_settings_routes_409_without_a_bot(
        self, streamer_client: TestClient
    ) -> None:
        for method, path, body in (
            ("get", "/bot/settings", None),
            ("post", "/bot/settings", {}),
            ("post", "/bot/trust", {"trusted": True}),
            ("post", "/bot/retract", {"handle": "abcd"}),
        ):
            call = getattr(streamer_client, method)
            response = call(path, json=body) if body is not None else call(path)
            assert response.status_code == 409, path


class TestSessionConfigRoutes:
    @staticmethod
    def _install_session(client: TestClient):
        from tests.test_sessions import FakePipeline

        class LivePipeline(FakePipeline):
            sensitivity = "medium"
            sends_transcripts = False
            enabled_topics = frozenset({"other"})

            def __init__(self) -> None:
                super().__init__("s1", "twitch", "teststreamer")
                self.applied: list[dict] = []

            def apply_live_config(self, **kwargs) -> None:
                self.applied.append(kwargs)
                if kwargs.get("sensitivity"):
                    self.sensitivity = kwargs["sensitivity"]
                if kwargs.get("send_transcripts") is not None:
                    self.sends_transcripts = kwargs["send_transcripts"]

        pipeline = LivePipeline()
        registry = client.app.state.sessions
        registry._sessions[pipeline.session_id] = pipeline
        return pipeline

    def test_get_reports_live_sessions_and_the_vision_gate(
        self, streamer_client: TestClient
    ) -> None:
        self._install_session(streamer_client)
        state = streamer_client.get("/session/config").json()
        assert state["vision_enabled"] is True  # server default
        assert state["sessions"][0]["sensitivity"] == "medium"

    def test_sensitivity_applies_to_the_live_session(
        self, streamer_client: TestClient
    ) -> None:
        """The extension's live-apply, panel-shaped: takes effect on the next
        gate pass, not the next stream."""
        pipeline = self._install_session(streamer_client)
        state = streamer_client.post(
            "/session/config", json={"sensitivity": "high"}
        ).json()
        assert pipeline.applied[0]["sensitivity"] == "high"
        assert state["sessions"][0]["sensitivity"] == "high"

    def test_vision_toggle_mutates_both_settings_handles(
        self, streamer_client: TestClient
    ) -> None:
        """A credentials hot-swap gives the LLM runtime a fresh Settings
        object; updating only one handle would make the toggle silently stop
        working after a key change."""
        streamer_client.post("/session/config", json={"vision_enabled": False})
        assert streamer_client.app.state.settings.vision_enabled is False
        assert streamer_client.app.state.llm_runtime.settings.vision_enabled is False

    def test_absent_fields_touch_nothing(self, streamer_client: TestClient) -> None:
        pipeline = self._install_session(streamer_client)
        streamer_client.post("/session/config", json={"vision_enabled": True})
        assert pipeline.applied == []  # no session field given: no session call

    def test_transcript_toggle_reaches_the_session(
        self, streamer_client: TestClient
    ) -> None:
        pipeline = self._install_session(streamer_client)
        streamer_client.post("/session/config", json={"send_transcripts": True})
        assert pipeline.applied[0]["send_transcripts"] is True

    def test_no_live_session_is_fine(self, streamer_client: TestClient) -> None:
        state = streamer_client.post(
            "/session/config", json={"sensitivity": "low"}
        ).json()
        assert state["sessions"] == []


class TestOverlayConfig:
    def test_defaults_when_nothing_stored(self, streamer_client: TestClient) -> None:
        config = streamer_client.get("/overlay/config").json()
        assert config["style"] == "toast"
        assert config["labels"] == ["FALSE", "MISLEADING"]
        assert config["duration_s"] == 14

    def test_post_persists_and_reads_back(self, streamer_client: TestClient) -> None:
        posted = streamer_client.post(
            "/overlay/config",
            json={"style": "lowerthird", "duration_s": 8, "max_stack": 2},
        )
        assert posted.status_code == 200
        fetched = streamer_client.get("/overlay/config").json()
        assert fetched["style"] == "lowerthird"
        assert fetched["duration_s"] == 8
        assert fetched["position"] == "bottom-left"  # untouched default

    def test_post_pushes_a_live_hub_frame(self, streamer_client: TestClient) -> None:
        """A live overlay restyles without a reload — the OBS source URL
        never changes."""
        hub: EventHub = streamer_client.app.state.events
        subscription = hub.subscribe(name="test", types={"overlay_config"})
        streamer_client.post("/overlay/config", json={"style": "stamp"})
        event = subscription._queue.get_nowait()
        assert event.frame["type"] == "overlay_config"
        assert event.frame["config"]["style"] == "stamp"

    def test_invalid_values_are_422_with_reasons(
        self, streamer_client: TestClient
    ) -> None:
        assert (
            streamer_client.post(
                "/overlay/config", json={"style": "hologram"}
            ).status_code
            == 422
        )
        assert (
            streamer_client.post("/overlay/config", json={"duration_s": 2}).status_code
            == 422
        )


class TestConsoleStats:
    def test_shape_on_an_empty_database(self, streamer_client: TestClient) -> None:
        stats = streamer_client.get("/stats/console").json()
        assert stats["approval_7d"]["rate"] is None
        assert stats["latency_today"] == {"median_ms": None, "n": 0}
        assert stats["funnel_today"]["heard"] == 0
        assert stats["live"] == []


class TestBotStatusAdditions:
    def test_review_ttl_is_exposed(self, streamer_client: TestClient) -> None:
        install_bot(streamer_client)
        status = streamer_client.get("/bot/status").json()
        assert status["bot"]["review_ttl_s"] == 180.0


class TestSessionConfigTopics:
    def test_enabled_topics_are_readable(self, streamer_client: TestClient) -> None:
        from tests.test_sessions import FakePipeline

        class TopicPipeline(FakePipeline):
            sensitivity = "medium"
            sends_transcripts = False
            enabled_topics = frozenset({"sports", "other"})

        pipeline = TopicPipeline("s1", "twitch", "teststreamer")
        streamer_client.app.state.sessions._sessions[pipeline.session_id] = pipeline
        state = streamer_client.get("/session/config").json()
        assert state["sessions"][0]["enabled_topics"] == ["other", "sports"]
