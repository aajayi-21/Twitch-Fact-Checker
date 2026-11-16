"""The app's sample check, POST /setup/test-check (app/text_check.py).

It shares /debug/text's checking path, so the gate/verify edge cases live in
test_ws_protocol.py::TestDebugText. These tests pin what is different: it is
always available, and it never pushes verdicts into live sessions.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import (
    FakeLLMClient,
    FakeTranscriber,
    make_gate_response,
    make_hello,
    make_test_settings,
    make_verdict_completion,
    open_test_client,
)

CLAIM = "The Eiffel Tower is 450 meters tall."


@pytest.fixture()
def release_client(
    fake_llm_client: FakeLLMClient, fake_transcriber: FakeTranscriber
) -> Iterator[TestClient]:
    """The app as it ships: debug endpoints off."""
    settings = make_test_settings(debug_endpoints=False)
    with open_test_client(settings, fake_llm_client, fake_transcriber) as client:
        yield client


def queue_one_false_verdict(fake_llm_client: FakeLLMClient) -> None:
    fake_llm_client.gate_results.append(make_gate_response([(CLAIM, 0.9)]))
    fake_llm_client.verify_results.append(
        make_verdict_completion(
            "FALSE",
            "The Eiffel Tower is about 330 meters tall.",
            citations=[("https://www.toureiffel.paris/x", "Key figures")],
        )
    )


class TestSampleCheck:
    def test_works_with_debug_endpoints_off(
        self, release_client: TestClient, fake_llm_client: FakeLLMClient
    ) -> None:
        queue_one_false_verdict(fake_llm_client)
        response = release_client.post(
            "/setup/test-check",
            json={"text": "the eiffel tower is 450 meters tall", "sensitivity": "high"},
        )
        assert response.status_code == 200
        (verdict,) = response.json()["verdicts"]
        assert verdict["label"] == "FALSE"
        assert release_client.post("/debug/text", json={"text": "x"}).status_code == 404

    def test_never_pushes_verdicts_into_a_live_session(
        self, release_client: TestClient, fake_llm_client: FakeLLMClient
    ) -> None:
        with release_client.websocket_connect("/ws/audio") as live:
            live.send_json(make_hello())
            assert live.receive_json()["type"] == "ready"
            queue_one_false_verdict(fake_llm_client)
            response = release_client.post(
                "/setup/test-check", json={"text": "the eiffel tower is 450 m"}
            )
            assert response.status_code == 200
            assert len(response.json()["verdicts"]) == 1
            # The final flush pass on stop finds nothing new to check.
            fake_llm_client.gate_results.append(make_gate_response([]))
            live.send_json({"type": "stop"})
            frames = []
            while True:
                try:
                    frames.append(live.receive_json())
                except WebSocketDisconnect:
                    break
        assert not [frame for frame in frames if frame.get("type") == "verdict"]

    def test_empty_text_is_400(self, release_client: TestClient) -> None:
        response = release_client.post("/setup/test-check", json={"text": "  "})
        assert response.status_code == 400

    def test_no_key_yet_is_409_with_guidance(
        self, fake_llm_client: FakeLLMClient, fake_transcriber: FakeTranscriber
    ) -> None:
        settings = make_test_settings(openrouter_api_key="", debug_endpoints=False)
        with open_test_client(settings, fake_llm_client, fake_transcriber) as client:
            response = client.post("/setup/test-check", json={"text": CLAIM})
        assert response.status_code == 409
        assert "API key" in response.json()["detail"]
