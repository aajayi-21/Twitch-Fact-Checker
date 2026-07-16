"""The whole Pydantic vocabulary: LLM schemas, verdicts, and wire frames.

Every JSON object that crosses a process boundary (WebSocket frames, LLM
structured output, debug endpoint bodies) is modelled here so that the wire
protocol has exactly one source of truth.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

logger = logging.getLogger(__name__)

Label = Literal["TRUE", "FALSE", "MISLEADING", "UNVERIFIED"]

Sensitivity = Literal["low", "medium", "high"]

Topic = Literal[
    "politics",
    "health",
    "science_tech",
    "money",
    "history",
    "sports",
    "gaming",
    "entertainment",
    "other",
]

# Canonical slug order (wire contract): the single source of truth consumed by
# the OpenRouter strict gate schema and the enabled-topics resolver. "other"
# is the catch-all bucket and can never be disabled.
TOPICS: tuple[Topic, ...] = (
    "politics",
    "health",
    "science_tech",
    "money",
    "history",
    "sports",
    "gaming",
    "entertainment",
    "other",
)

_TOPIC_SET: frozenset[str] = frozenset(TOPICS)


def resolve_enabled_topics(enabled_topics: list[str] | None) -> frozenset[str]:
    """The enabled topic-slug set for a session or debug request.

    ``None`` (field omitted) means every topic is enabled. Unknown slugs are
    ignored with a WARNING, never an error, and ``"other"`` is ALWAYS enabled
    so misclassified claims keep a live bucket.
    """
    if enabled_topics is None:
        return _TOPIC_SET
    unknown = sorted(set(enabled_topics) - _TOPIC_SET)
    if unknown:
        logger.warning("ignoring unknown topic slugs: %s", ", ".join(unknown))
    return frozenset(slug for slug in enabled_topics if slug in _TOPIC_SET) | {"other"}


ErrorCode = Literal[
    "bad_hello",
    "stt_overload",
    "llm_failure",
    "rate_limited",
    "quota_cooldown",
    "superseded",
]


def utc_now_iso() -> str:
    """Current UTC time as ``2026-07-15T20:01:11Z`` (wire format for verdicts)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_verdict_id() -> str:
    """Opaque unique verdict id (hex uuid4)."""
    return uuid4().hex


# --------------------------------------------------------------------------- #
# Client -> server frames (§2.1)
# --------------------------------------------------------------------------- #


class ClientHello(BaseModel):
    """First text frame on /ws/audio; strict — a bad hello closes with 1008."""

    type: Literal["hello"]
    version: Literal[1]
    format: Literal["pcm_s16le"]
    sample_rate: Literal[16000]
    channels: Literal[1]
    sensitivity: Sensitivity = "medium"
    # None (or omitted) = all topics enabled. Typed list[str], not
    # list[Topic]: unknown slugs must be IGNORED with a warning at resolve
    # time (:func:`resolve_enabled_topics`), never rejected with a 1008.
    enabled_topics: list[str] | None = None
    send_transcripts: bool = True


class ClientConfig(BaseModel):
    """Mid-session settings update, e.g. ``{"type":"config","sensitivity":"high"}``.

    Every field is optional and applied independently: a sensitivity-only
    frame leaves the topic filter untouched and vice versa.
    """

    type: Literal["config"]
    sensitivity: Sensitivity | None = None
    enabled_topics: list[str] | None = None


class ClientStop(BaseModel):
    """Graceful end: server flushes, runs a final gate pass, closes 1000."""

    type: Literal["stop"]


# --------------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------------- #


class TranscriptSegment(BaseModel):
    """One filtered Whisper segment; times are absolute stream seconds."""

    text: str
    start: float
    end: float
    avg_logprob: float
    no_speech_prob: float


# --------------------------------------------------------------------------- #
# LLM structured-output schemas (kept deliberately flat — see plan §4)
# --------------------------------------------------------------------------- #


class GateClaim(BaseModel):
    """One claim extracted by the gate model."""

    claim_text: str
    check_worthiness: float = Field(ge=0.0, le=1.0)
    # Deliberately NO default: the field must appear in the JSON schema's
    # `required` list so schema-enforced providers (Gemini response_schema,
    # OpenRouter strict json_schema) force the model to emit it. The
    # before-validator below still supplies "other" whenever a non-strict
    # parse path omits the key.
    topic: Topic

    @model_validator(mode="before")
    @classmethod
    def _default_unknown_topic(cls, data: Any) -> Any:
        """Missing/unknown model output coerces to "other" (wire contract).

        Case/whitespace variants of canonical slugs (e.g. "Politics") are
        normalized rather than discarded: they can only arrive via the
        non-schema-enforced parse paths (OpenRouter json_object mode, Gemini
        raw-text fallback), where the enum is prompt-suggested only.
        """
        if not isinstance(data, dict):
            return data
        value = data.get("topic")
        if isinstance(value, str):
            candidate = value.strip().lower()
            if candidate in _TOPIC_SET:
                if candidate != value:
                    return {**data, "topic": candidate}
                return data
        if value is not None:
            logger.warning("coercing unknown gate topic %r to 'other'", value)
        return {**data, "topic": "other"}


class GateResult(BaseModel):
    """`response_schema` for the claim-gate call."""

    claims: list[GateClaim]


class Source(BaseModel):
    """A citation extracted from grounding metadata (never from model JSON)."""

    url: str
    title: str | None = None


class VerdictPayload(BaseModel):
    """The flat two-field schema the verify model must return.

    Deliberately minimal: complex schemas combined with search grounding are
    the known 400 sharp edge.
    """

    label: Label
    explanation: str


class Verdict(BaseModel):
    """A fully-assembled fact-check result."""

    id: str = Field(default_factory=new_verdict_id)
    claim: str
    topic: Topic = "other"
    label: Label
    explanation: str
    sources: list[Source]
    checked_at: str = Field(default_factory=utc_now_iso)
    used_fallback: bool = False


# --------------------------------------------------------------------------- #
# Server -> client frames (§2.1)
# --------------------------------------------------------------------------- #


class ReadyFrame(BaseModel):
    """Sent once after a valid hello."""

    type: Literal["ready"] = "ready"
    server_version: str
    model: str


class TranscriptFrame(BaseModel):
    """Emitted per filtered segment when the client opted into transcripts."""

    type: Literal["transcript"] = "transcript"
    text: str
    start: float
    end: float


class StatusFrame(BaseModel):
    """Per-claim progress frames.

    - ``"verifying"``: fires once per verification start -> overlay
      'Checking claim…' chip (unchanged semantics; carries no topic key).
    - ``"topic_skipped"``: fires once per claim dropped by the topic filter
      INSTEAD of verification (no verdict follows); carries the claim's topic.
    """

    type: Literal["status"] = "status"
    stage: Literal["verifying", "topic_skipped"] = "verifying"
    claim: str
    topic: Topic | None = None

    @model_serializer(mode="wrap")
    def _omit_null_topic(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Keep "verifying" frames byte-identical to the pre-topic wire shape."""
        data = handler(self)
        if data.get("topic") is None:
            data.pop("topic", None)
        return data


class VerdictFrame(BaseModel):
    """A :class:`Verdict` on the wire."""

    type: Literal["verdict"] = "verdict"
    id: str
    claim: str
    topic: Topic
    label: Label
    explanation: str
    sources: list[Source]
    checked_at: str
    used_fallback: bool

    @classmethod
    def from_verdict(cls, verdict: Verdict) -> "VerdictFrame":
        return cls(**verdict.model_dump())


class ErrorFrame(BaseModel):
    """Non-fatal errors keep the session alive; fatal ones precede a close."""

    type: Literal["error"] = "error"
    code: ErrorCode
    message: str
    fatal: bool = False


# --------------------------------------------------------------------------- #
# Debug endpoint (§3.2)
# --------------------------------------------------------------------------- #


class DebugTextRequest(BaseModel):
    """Body for POST /debug/text — the no-audio test path."""

    text: str
    sensitivity: Sensitivity = "medium"
    # None = all topics enabled (same semantics as the hello frame).
    enabled_topics: list[str] | None = None


class DebugTextResponse(BaseModel):
    """Everything the gate found plus verdicts for claims that passed."""

    claims: list[GateClaim]
    verdicts: list[Verdict]
