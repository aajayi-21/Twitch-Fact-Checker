"""End-to-end WS persistence: identity, funnel, latency, counters.

Conventions from test_ws_protocol.py: scripted fakes drive a real session
(hello → audio → stop → collect); DB assertions use FRESH sqlite3 read
connections polled with a sync wait helper, because the session-end write
lands in the server's ``finally`` AFTER the client observes the close.
"""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.llm_gemini import GeminiClaimGate, GeminiFactChecker
from app.models import ClientHello, GateClaim, TranscriptSegment
from app.pipeline import SessionPipeline
from app.rate_limit import QuotaCooldown, TokenBucket
from tests.conftest import (
    FakeGenAIClient,
    FakeTranscriber,
    make_gate_response,
    make_hello,
    make_test_settings,
    make_verdict_interaction,
    pcm_silence,
)

EIGHT_WORD_SEGMENT = TranscriptSegment(
    text="the eiffel tower is three hundred thirty meters",
    start=0.0,
    end=1.0,
    avg_logprob=-0.3,
    no_speech_prob=0.05,
)


def wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s}s")


def collect_frames_until_close(session: Any) -> tuple[list[dict[str, Any]], int]:
    from starlette.websockets import WebSocketDisconnect

    frames: list[dict[str, Any]] = []
    while True:
        try:
            frames.append(session.receive_json())
        except WebSocketDisconnect as disconnect:
            return frames, disconnect.code


def rows(db_path: str, query: str) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(query).fetchall()


class TestSessionPersistence:
    def test_identity_funnel_latency_and_counters(
        self,
        client: TestClient,
        app_settings: Settings,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        fake_transcriber.segments_script.append([EIGHT_WORD_SEGMENT])
        fake_genai_client.generate_results.append(
            make_gate_response(
                [
                    # Order matters: the verified claim precedes its
                    # paraphrase so dedupe drops the second.
                    ("Politicians always lie about their tax plans.", 0.2, "politics"),
                    ("Brazil has won five FIFA World Cups.", 0.9, "sports"),
                    ("The Eiffel Tower is 330 meters tall.", 0.9, "history"),
                    ("The Eiffel Tower is 330 meters in height.", 0.9, "history"),
                ]
            )
        )
        fake_genai_client.interaction_results.append(
            make_verdict_interaction(
                "TRUE",
                "Official figure including antennas.",
                citations=[("https://www.toureiffel.paris/facts", "Key figures")],
            )
        )
        hello = make_hello(
            platform="twitch",
            channel="somechannel",
            stream_title="Reacting to news!",
            enabled_topics=[  # sports OFF
                "politics",
                "health",
                "science_tech",
                "money",
                "history",
                "gaming",
                "entertainment",
            ],
        )
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(hello)
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)
        assert close_code == 1000
        assert [f["label"] for f in frames if f["type"] == "verdict"] == ["TRUE"]

        db_path = app_settings.db_path
        wait_until(
            lambda: rows(
                db_path, "SELECT ended_at FROM sessions WHERE ended_at IS NOT NULL"
            )
            != []
        )
        session_row = rows(
            db_path,
            "SELECT platform, channel, title, gate_calls, verify_calls,"
            " est_cost_usd, speech_seconds FROM sessions",
        )[0]
        assert session_row[0] == "twitch"
        assert session_row[1] == "somechannel"
        assert session_row[2] == "Reacting to news!"
        assert session_row[3] == 1  # one real gate call (final flush pass)
        assert session_row[4] == 1  # one verify attempt
        assert session_row[5] == 0.005
        assert session_row[6] > 0  # speech seconds accumulated

        outcomes = dict(rows(db_path, "SELECT text, outcome FROM claims"))
        assert outcomes == {
            "Politicians always lie about their tax plans.": "below_threshold",
            "Brazil has won five FIFA World Cups.": "topic_skipped",
            "The Eiffel Tower is 330 meters tall.": "verified",
            "The Eiffel Tower is 330 meters in height.": "duplicate",
        }
        verdict_row = rows(
            db_path,
            "SELECT label, latency_ms, provider, model FROM verdicts",
        )[0]
        assert verdict_row[0] == "TRUE"
        assert verdict_row[1] >= 0
        assert verdict_row[2] == "gemini"
        assert verdict_row[3] == "fake-verify-model"
        assert rows(db_path, "SELECT url FROM sources") == [
            ("https://www.toureiffel.paris/facts",)
        ]
        health = client.get("/healthz").json()
        assert health["checks_today"] == 1
        assert health["est_cost_today_usd"] == 0.005

    def test_verify_failure_records_outcome_and_session_survives(
        self,
        client: TestClient,
        app_settings: Settings,
        fake_genai_client: FakeGenAIClient,
        fake_transcriber: FakeTranscriber,
    ) -> None:
        from tests.conftest import FakeInteractionsError

        fake_transcriber.segments_script.append([EIGHT_WORD_SEGMENT])
        fake_genai_client.generate_results.append(
            make_gate_response([("The Eiffel Tower is 330 meters tall.", 0.9)])
        )
        fake_genai_client.interaction_results.append(
            FakeInteractionsError(500, "verify exploded")
        )
        with client.websocket_connect("/ws/audio") as session:
            session.send_json(make_hello())
            assert session.receive_json()["type"] == "ready"
            for _ in range(4):
                session.send_bytes(pcm_silence(0.25))
            session.send_json({"type": "stop"})
            frames, close_code = collect_frames_until_close(session)
        assert close_code == 1000  # per-item failure never kills the session
        assert any(f["type"] == "error" and f["code"] == "llm_failure" for f in frames)
        db_path = app_settings.db_path
        wait_until(
            lambda: rows(
                db_path, "SELECT 1 FROM claims WHERE outcome = 'verify_failed'"
            )
            != []
        )
        assert rows(db_path, "SELECT COUNT(*) FROM verdicts") == [(0,)]


class TestQueueDroppedOutcome:
    async def test_dropped_oldest_claim_is_recorded(self, tmp_path) -> None:
        """Unit-level: the verify queue's drop-OLDEST path writes the
        queue_dropped outcome (à la TestShutdownEnqueueContract)."""
        db = Database(str(tmp_path / "queue.db"))
        await db.open()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-unit")
        try:
            fake_client = FakeGenAIClient()
            settings = make_test_settings(db_path=str(tmp_path / "queue.db"))
            pipeline = SessionPipeline(
                websocket=SimpleNamespace(),  # type: ignore[arg-type]
                hello=ClientHello.model_validate(make_hello()),
                settings=settings,
                transcriber=FakeTranscriber(),  # type: ignore[arg-type]
                stt_executor=executor,
                claim_gate=GeminiClaimGate(client=fake_client, model="fake-gate-model"),
                fact_checker=GeminiFactChecker(
                    client=fake_client,
                    verify_model="fake-verify-model",
                    extraction_model="fake-gate-model",
                    cooldown=QuotaCooldown(),
                ),
                verify_bucket=TokenBucket(rate_per_min=6000.0, burst=10),
                quota_cooldown=QuotaCooldown(),
                db=db,
            )
            # run() normally writes the session row before any claim; this
            # unit test bypasses run(), so seed it (claims FK sessions.id).
            await db.record_session_start(
                session_id=pipeline._session_id,
                platform=None,
                channel=None,
                title=None,
            )
            texts = [
                "Claim one is all about apples.",
                "Claim two is all about bridges.",
                "Claim three is all about comets.",
                "Claim four is all about dragons.",
            ]
            for text in texts:
                await pipeline._enqueue_claim(
                    GateClaim(claim_text=text, check_worthiness=0.9, topic="other")
                )
            outcome_rows = dict(
                rows(str(tmp_path / "queue.db"), "SELECT text, outcome FROM claims")
            )
            # Queue maxsize is 3: the OLDEST claim was evicted and marked.
            assert outcome_rows["Claim one is all about apples."] == "queue_dropped"
            assert outcome_rows["Claim four is all about dragons."] == "pending"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            await db.close()
