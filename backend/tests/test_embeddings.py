"""OllamaEmbedder: URL derivation, request shape, normalization, failures."""

import json

import httpx
import numpy as np
import pytest

from app.embeddings import EmbeddingUnavailable, OllamaEmbedder, ollama_native_base


class TestOllamaNativeBase:
    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434"),
            ("http://127.0.0.1:11434/v1/", "http://127.0.0.1:11434"),
            ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
            ("http://box.local:8080/v1", "http://box.local:8080"),
        ],
    )
    def test_strips_exactly_one_v1_suffix(self, base_url: str, expected: str) -> None:
        assert ollama_native_base(base_url) == expected


def make_embedder(handler) -> OllamaEmbedder:  # type: ignore[no-untyped-def]
    return OllamaEmbedder(
        "http://127.0.0.1:11434/v1",
        "fake-embed-model",
        transport=httpx.MockTransport(handler),
    )


class TestEmbed:
    async def test_happy_path_request_shape_and_unit_norm(self) -> None:
        seen_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            return httpx.Response(200, json={"embeddings": [[3.0, 4.0], [0.0, 2.0]]})

        vectors = await make_embedder(handler).embed(["first", "second"])
        request = seen_requests[0]
        assert str(request.url) == "http://127.0.0.1:11434/api/embed"
        assert json.loads(request.content) == {
            "model": "fake-embed-model",
            "input": ["first", "second"],
        }
        assert len(vectors) == 2
        for vector in vectors:
            assert vector.dtype == np.float32
            assert np.linalg.norm(vector) == pytest.approx(1.0)
        # Direction preserved: [3,4] -> [0.6, 0.8].
        assert vectors[0] == pytest.approx([0.6, 0.8])

    async def test_non_200_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="model not found")

        with pytest.raises(EmbeddingUnavailable, match="HTTP 404"):
            await make_embedder(handler).embed(["text"])

    async def test_malformed_body_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"nope": []})

        with pytest.raises(EmbeddingUnavailable, match="malformed"):
            await make_embedder(handler).embed(["text"])

    async def test_count_mismatch_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})

        with pytest.raises(EmbeddingUnavailable, match="shape mismatch"):
            await make_embedder(handler).embed(["one", "two"])

    async def test_connect_error_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(EmbeddingUnavailable, match="could not reach"):
            await make_embedder(handler).embed(["text"])
