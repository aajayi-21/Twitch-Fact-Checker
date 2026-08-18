"""Tests for the ingest CLI's pure parts: identity, argv, error policy, hello.

The subprocess and socket layers are exercised live (the CLI against a
running backend); everything decision-shaped is pure and tested here.
"""

import wave
from pathlib import Path

import numpy as np
import pytest

from streamer.ingest import FRAME_BYTES, SAMPLE_RATE
from streamer.ingest.client import Action, build_hello, classify_error
from streamer.ingest.cli import build_parser, parse_topics, resolve_source
from streamer.ingest.sources import (
    Identity,
    WavSource,
    derive_identity,
    ffmpeg_argv,
    load_pcm_16k_mono,
    streamlink_argv,
)


class TestDeriveIdentity:
    @pytest.mark.parametrize(
        ("target", "platform", "channel"),
        [
            ("somestreamer", "twitch", "somestreamer"),
            ("SomeStreamer", "twitch", "somestreamer"),
            ("twitch.tv/foo", "twitch", "foo"),
            ("https://www.twitch.tv/Foo?ref=x", "twitch", "foo"),
            ("kick.com/bar", "kick", "bar"),
            # YouTube/Rumble URLs carry video ids, not channel names.
            ("https://www.youtube.com/watch?v=abc123", "youtube", None),
            ("https://rumble.com/v12345-title.html", "rumble", None),
            ("https://example.com/foo", None, None),
            ("", None, None),
        ],
    )
    def test_table(self, target: str, platform, channel) -> None:
        assert derive_identity(target) == Identity(platform, channel)

    @pytest.mark.parametrize("hostile", ["../etc", "foo;rm -rf", "a b", "x" * 30])
    def test_hostile_path_segments_never_become_channels(self, hostile: str) -> None:
        """Channel names go into argv (never a shell) — but they are still
        validated against Twitch's login alphabet as belt and braces."""
        identity = derive_identity(f"twitch.tv/{hostile}")
        assert identity.channel is None


class TestStreamlinkArgv:
    def test_golden_argv_proves_no_shell(self) -> None:
        """The exact list. If someone rewrites this as a shell string, this
        test is the tripwire."""
        assert streamlink_argv("twitch.tv/foo", "audio_only,worst", []) == [
            "streamlink",
            "--stdout",
            "--twitch-disable-ads",
            "--retry-streams",
            "30",
            "--retry-max",
            "0",
            "twitch.tv/foo",
            "audio_only,worst",
        ]

    def test_extra_args_pass_through_before_the_target(self) -> None:
        argv = streamlink_argv("twitch.tv/foo", "worst", ["--twitch-low-latency"])
        assert "--twitch-low-latency" in argv
        assert argv.index("--twitch-low-latency") < argv.index("twitch.tv/foo")

    def test_ffmpeg_argv_has_nostdin_and_16k_mono(self) -> None:
        argv = ffmpeg_argv()
        # Without -nostdin, ffmpeg eats the terminal's stdin and breaks ^C.
        assert "-nostdin" in argv
        assert argv[argv.index("-ar") + 1] == "16000"
        assert argv[argv.index("-ac") + 1] == "1"


class TestClassifyError:
    @pytest.mark.parametrize(
        ("code", "action"),
        [
            ("superseded", Action.FATAL),
            ("not_configured", Action.FATAL),
            ("bad_hello", Action.FATAL),
            ("too_many_sessions", Action.FATAL),
            ("credentials_updated", Action.RECONNECT),
            ("stt_overload", Action.CONTINUE),
            ("rate_limited", Action.CONTINUE),
            ("quota_cooldown", Action.CONTINUE),
            ("llm_failure", Action.CONTINUE),
            ("some_future_code", Action.CONTINUE),
        ],
    )
    def test_policy_table(self, code: str, action: Action) -> None:
        assert classify_error({"type": "error", "code": code}).action is action

    def test_superseded_is_terminal_to_prevent_preempt_ping_pong(self) -> None:
        """If a browser tab owns the session, a CLI that auto-reconnected
        would preempt it, get preempted back, and loop every half second."""
        policy = classify_error({"type": "error", "code": "superseded"})
        assert policy.action is Action.FATAL
        assert "one session at a time" in policy.message

    def test_unknown_fatal_frames_are_fatal(self) -> None:
        policy = classify_error(
            {"type": "error", "code": "brand_new", "fatal": True, "message": "x"}
        )
        assert policy.action is Action.FATAL


class TestHello:
    def test_carries_identity(self) -> None:
        """THE stream_wav.py bug this module fixes: no platform/channel meant
        sessions invisible to /stats/channels and unusable for the bot's
        channel binding."""
        hello = build_hello(identity=Identity("twitch", "foo"))
        assert hello["platform"] == "twitch"
        assert hello["channel"] == "foo"
        assert hello["format"] == "pcm_s16le"
        assert hello["sample_rate"] == SAMPLE_RATE

    def test_transcripts_default_off(self) -> None:
        """The extension defaults transcripts ON for its private overlay; a
        headless CLI has no overlay, so it opts out unless asked."""
        assert build_hello(identity=Identity(None, None))["send_transcripts"] is False

    def test_topics_included_only_when_given(self) -> None:
        bare = build_hello(identity=Identity(None, None))
        assert "enabled_topics" not in bare
        with_topics = build_hello(
            identity=Identity(None, None), enabled_topics=["sports"]
        )
        assert with_topics["enabled_topics"] == ["sports"]


def write_wav(path: Path, *, rate: int = SAMPLE_RATE, channels: int = 1) -> None:
    samples = (np.sin(np.linspace(0, 100, rate)) * 1000).astype("<i2")
    if channels > 1:
        samples = np.repeat(samples[:, None], channels, axis=1).reshape(-1)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples.tobytes())


class TestWavSource:
    async def test_yields_full_frames_at_speed(self, tmp_path: Path) -> None:
        path = tmp_path / "one-second.wav"
        write_wav(path)
        source = WavSource(path, speed=1000.0)  # effectively unpaced
        frames = [frame async for frame in source.frames()]
        assert len(frames) == 4  # 1 s / 250 ms
        assert all(len(frame) == FRAME_BYTES for frame in frames[:-1])

    def test_loader_resamples_and_downmixes(self, tmp_path: Path) -> None:
        path = tmp_path / "stereo-44k.wav"
        write_wav(path, rate=44100, channels=2)
        samples = load_pcm_16k_mono(path)
        assert abs(len(samples) - SAMPLE_RATE) <= 2  # ~1 s at 16 kHz

    def test_non_16bit_wav_is_a_clear_error(self, tmp_path: Path) -> None:
        path = tmp_path / "eight-bit.wav"
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(1)
            writer.setframerate(SAMPLE_RATE)
            writer.writeframes(b"\x80" * SAMPLE_RATE)
        with pytest.raises(SystemExit, match="16-bit"):
            load_pcm_16k_mono(path)


class TestCliSurface:
    def test_auto_resolves_wav_paths(self, tmp_path: Path) -> None:
        path = tmp_path / "fixture.wav"
        write_wav(path)
        args = build_parser().parse_args([str(path)])
        source, identity = resolve_source(args)
        assert source.name == "wav"
        assert identity == Identity(None, None)

    def test_channel_override_wins(self, tmp_path: Path) -> None:
        path = tmp_path / "fixture.wav"
        write_wav(path)
        args = build_parser().parse_args(
            [str(path), "--source", "wav", "--channel", "#SomeOne"]
        )
        _, identity = resolve_source(args)
        assert identity == Identity("twitch", "someone")

    def test_no_target_no_source_is_a_clear_error(self) -> None:
        args = build_parser().parse_args([])
        with pytest.raises(SystemExit, match="channel/URL"):
            resolve_source(args)

    def test_unknown_topics_are_rejected_with_the_valid_list(self) -> None:
        with pytest.raises(SystemExit, match="astrology"):
            parse_topics("sports,astrology")
        assert parse_topics("sports, health") == ["sports", "health"]
        assert parse_topics(None) is None
