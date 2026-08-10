"""Sentence embeddings via Ollama's native ``/api/embed`` endpoint.

Used by contradiction detection to retrieve candidate claim pairs before the
LLM judge call. Chat traffic uses the OpenAI-compatible ``.../v1`` root;
embeddings live on the NATIVE root — :func:`ollama_native_base` derives one
from the other so a single ``OLLAMA_BASE_URL`` setting covers both.

Deliberately no persistent HTTP client: one probe-sized request every ~12 s
per session does not justify lifecycle management (the setup probes follow
the same pattern). Every failure mode maps to :class:`EmbeddingUnavailable`
so the caller can degrade to lexical matching with a single except clause.
"""

import logging

import httpx
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_EMBED_TIMEOUT_S = 10.0


class EmbeddingUnavailable(Exception):
    """The embedding server could not be reached or answered unusably."""


def ollama_native_base(base_url: str) -> str:
    """The native API root for an OpenAI-compatible ``.../v1`` base URL.

    Strips exactly ONE trailing ``/v1`` (after trimming trailing slashes);
    a URL without the suffix is returned trimmed as-is. A base URL missing
    ``/v1`` breaks CHAT, not embeddings — the setup reachability probe
    catches that misconfiguration.
    """
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        return trimmed[: -len("/v1")]
    return trimmed


class OllamaEmbedder:
    """Batch text embedding against ``POST {native}/api/embed``."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = DEFAULT_EMBED_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._embed_url = f"{ollama_native_base(base_url)}/api/embed"
        self._model = model
        self._timeout_s = timeout_s
        # Test seam: httpx.MockTransport injection instead of monkeypatching.
        self._transport = transport

    async def embed(self, texts: list[str]) -> list[np.ndarray]:
        """Unit-normalized float32 vectors, one per input text.

        Raises:
            EmbeddingUnavailable: on network failure, non-200, or a response
                whose shape/lengths do not match the request.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as http_client:
                response = await http_client.post(
                    self._embed_url,
                    json={"model": self._model, "input": texts},
                )
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(
                f"could not reach the embedding server at {self._embed_url}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise EmbeddingUnavailable(
                f"embedding request failed (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        try:
            embeddings = response.json()["embeddings"]
            vectors = [
                np.asarray(embedding, dtype=np.float32) for embedding in embeddings
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingUnavailable(
                f"malformed embedding response: {exc}"
            ) from exc
        if len(vectors) != len(texts) or any(v.ndim != 1 or not v.size for v in vectors):
            raise EmbeddingUnavailable(
                f"embedding response shape mismatch: {len(vectors)} vectors "
                f"for {len(texts)} inputs"
            )
        return [_unit_normalize(vector) for vector in vectors]


def _unit_normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return vector
    return vector / norm
