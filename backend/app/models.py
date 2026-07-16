"""The whole Pydantic vocabulary: LLM schemas, verdicts, and wire frames.

Every JSON object that crosses a process boundary (WebSocket frames, LLM
structured output, debug endpoint bodies) is modelled here so that the wire
protocol has exactly one source of truth.
"""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

Label = Literal["TRUE", "FALSE", "MISLEADING", "UNVERIFIED"]

Sensitivity = Literal["low", "medium", "high"]

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
    send_transcripts: bool = True


class ClientConfig(BaseModel):
    """Mid-session settings update, e.g. ``{"type":"config","sensitivity":"high"}``."""

    type: Literal["config"]
    sensitivity: Sensitivity


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
    """Fires once per verification start -> overlay 'Checking claim…' chip."""

    type: Literal["status"] = "status"
    stage: Literal["verifying"] = "verifying"
    claim: str


class VerdictFrame(BaseModel):
    """A :class:`Verdict` on the wire."""

    type: Literal["verdict"] = "verdict"
    id: str
    claim: str
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


class DebugTextResponse(BaseModel):
    """Everything the gate found plus verdicts for claims that passed."""

    claims: list[GateClaim]
    verdicts: list[Verdict]
