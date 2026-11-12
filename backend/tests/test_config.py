"""Settings / API-key startup checks and per-stage provider resolution.

Regression coverage for the ``.env.example`` inline-comment trap: python-dotenv
parses an inline comment after an EMPTY value as the value itself, so a
``.env`` copied verbatim from ``.env.example`` must still fail the startup
key check loudly.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    ANTHROPIC_EST_COST_PER_VERIFY_USD,
    DEFAULT_ANTHROPIC_MODEL,
    OPENROUTER_EST_COST_PER_VERIFY_USD,
    Settings,
    anthropic_sdk_credentials_available,
)

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


class TestRequireOpenRouterApiKey:
    def test_env_example_copied_verbatim_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        settings = Settings(_env_file=str(env_file))
        assert settings.llm_provider == "anthropic"  # .env.example default
        # No Anthropic key and (conftest-isolated) no `ant` sign-in.
        assert not settings.is_configured
        with pytest.raises(RuntimeError, match="ant auth login"):
            settings.require_llm_api_key()
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            settings.require_openrouter_api_key()

    def test_env_example_non_key_values_still_parse(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("WHISPER_MODEL", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        settings = Settings(_env_file=str(env_file))
        assert settings.whisper_model == "distil-small.en"

    @pytest.mark.parametrize(
        "bad_key",
        ["", "   ", "# required, backend-only, never in the extension"],
    )
    def test_empty_whitespace_or_comment_keys_are_rejected(self, bad_key: str) -> None:
        settings = Settings(openrouter_api_key=bad_key, _env_file=None)
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            settings.require_openrouter_api_key()

    def test_real_key_passes(self) -> None:
        settings = Settings(openrouter_api_key="sk-or-v1-plausible", _env_file=None)
        settings.require_openrouter_api_key()


class TestRequireLlmApiKey:
    def test_missing_openrouter_key_fails(self) -> None:
        settings = Settings(
            llm_provider="openrouter", openrouter_api_key="", _env_file=None
        )
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            settings.require_llm_api_key()

    def test_openrouter_key_passes(self) -> None:
        settings = Settings(
            llm_provider="openrouter",
            openrouter_api_key="sk-or-v1-plausible",
            _env_file=None,
        )
        settings.require_llm_api_key()

    def test_missing_anthropic_credentials_fail_with_both_options(self) -> None:
        # The conftest isolates the Anthropic config dir, so no sign-in exists.
        settings = Settings(_env_file=None)
        with pytest.raises(RuntimeError, match="ant auth login") as excinfo:
            settings.require_llm_api_key()
        assert "ANTHROPIC_API_KEY" in str(excinfo.value)

    def test_anthropic_key_passes(self) -> None:
        Settings(
            anthropic_api_key="sk-ant-plausible", _env_file=None
        ).require_llm_api_key()

    @pytest.mark.parametrize(
        "field", ["llm_provider", "gate_provider", "verify_provider"]
    )
    def test_removed_gemini_provider_is_rejected(self, field: str) -> None:
        # Gemini support was removed; an old .env naming it must fail at boot
        # rather than silently running on something else.
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: "gemini"})


class TestPerStageResolution:
    def test_empty_overrides_follow_llm_provider(self) -> None:
        settings = Settings(llm_provider="openrouter", _env_file=None)
        assert settings.resolved_gate_provider == "openrouter"
        assert settings.resolved_verify_provider == "openrouter"

    def test_anthropic_is_the_default_provider_with_haiku(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.llm_provider == "anthropic"
        assert settings.resolved_gate_provider == "anthropic"
        assert settings.resolved_verify_provider == "anthropic"
        assert (
            settings.active_gate_model == DEFAULT_ANTHROPIC_MODEL == "claude-haiku-5-5"
        )
        assert settings.active_verify_model == DEFAULT_ANTHROPIC_MODEL

    def test_gate_override_wins_over_llm_provider(self) -> None:
        settings = Settings(
            llm_provider="openrouter",
            gate_provider="ollama",
            verify_provider="openrouter",
            _env_file=None,
        )
        assert settings.resolved_gate_provider == "ollama"
        assert settings.resolved_verify_provider == "openrouter"

    def test_ollama_is_always_configured(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.provider_configured("ollama") is True
        assert settings.provider_configured("nonsense") is False

    def test_is_configured_needs_every_distinct_stage_provider(self) -> None:
        base = dict(
            llm_provider="openrouter",
            gate_provider="ollama",
            _env_file=None,
        )
        assert not Settings(openrouter_api_key="", **base).is_configured
        assert Settings(openrouter_api_key="sk-or-x", **base).is_configured

    def test_require_llm_api_key_skips_ollama(self) -> None:
        settings = Settings(
            llm_provider="openrouter",
            gate_provider="ollama",
            openrouter_api_key="sk-or-x",
            _env_file=None,
        )
        settings.require_llm_api_key()  # must not raise: ollama is keyless


def _write_profile(config_dir: Path, name: str, credentials: bool = True) -> None:
    """Create an `ant auth login`-shaped profile (files exist; contents unused)."""
    (config_dir / "configs").mkdir(parents=True, exist_ok=True)
    (config_dir / "configs" / f"{name}.json").write_text("{}", encoding="utf-8")
    if credentials:
        (config_dir / "credentials").mkdir(parents=True, exist_ok=True)
        (config_dir / "credentials" / f"{name}.json").write_text("{}", encoding="utf-8")


class TestAnthropicAuthDetection:
    """``ant auth login`` / SDK credential detection (env + file existence).

    The autouse ``_isolated_anthropic_credentials`` fixture points
    ANTHROPIC_CONFIG_DIR at an empty temp dir and clears the credential env
    vars, so each test starts with NO credentials.
    """

    @pytest.fixture
    def config_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        directory = tmp_path / "anthropic"
        monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(directory))
        return directory

    def test_nothing_configured(self, config_dir: Path) -> None:
        assert anthropic_sdk_credentials_available() is False
        settings = Settings(_env_file=None)
        assert settings.anthropic_auth_method is None
        assert settings.provider_configured("anthropic") is False
        assert settings.is_configured is False

    def test_default_profile_counts_as_sign_in(self, config_dir: Path) -> None:
        _write_profile(config_dir, "default")
        settings = Settings(_env_file=None)
        assert settings.anthropic_auth_method == "sign_in"
        assert settings.is_configured is True

    def test_config_without_credentials_is_not_a_sign_in(
        self, config_dir: Path
    ) -> None:
        _write_profile(config_dir, "default", credentials=False)
        assert anthropic_sdk_credentials_available() is False

    def test_active_config_selects_the_profile(self, config_dir: Path) -> None:
        _write_profile(config_dir, "work")
        assert anthropic_sdk_credentials_available() is False  # "default" absent
        (config_dir / "active_config").write_text("work\n", encoding="utf-8")
        assert anthropic_sdk_credentials_available() is True

    def test_anthropic_profile_env_wins(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_profile(config_dir, "default")
        monkeypatch.setenv("ANTHROPIC_PROFILE", "missing")
        assert anthropic_sdk_credentials_available() is False
        _write_profile(config_dir, "missing")
        assert anthropic_sdk_credentials_available() is True

    def test_auth_token_env_counts(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-offline-test")
        assert anthropic_sdk_credentials_available() is True

    def test_federation_env_needs_every_part(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_x")
        monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org_x")
        assert anthropic_sdk_credentials_available() is False
        monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN_FILE", "/var/run/token")
        assert anthropic_sdk_credentials_available() is True

    def test_a_key_wins_over_the_sign_in(self, config_dir: Path) -> None:
        _write_profile(config_dir, "default")
        settings = Settings(anthropic_api_key="sk-ant-x", _env_file=None)
        assert settings.anthropic_auth_method == "api_key"
        assert settings.anthropic_api_key_or_none == "sk-ant-x"

    @pytest.mark.parametrize("placeholder", ["", "  ", "# optional"])
    def test_placeholder_key_means_sign_in(self, placeholder: str) -> None:
        assert (
            Settings(
                anthropic_api_key=placeholder, _env_file=None
            ).anthropic_api_key_or_none
            is None
        )


class TestProviderCostDefault:
    def test_anthropic_verify_uses_the_anthropic_estimate(self) -> None:
        assert (
            Settings(_env_file=None).cost_per_verify_usd
            == ANTHROPIC_EST_COST_PER_VERIFY_USD
        )

    def test_openrouter_verify_uses_the_exa_estimate(self) -> None:
        settings = Settings(llm_provider="openrouter", _env_file=None)
        assert settings.cost_per_verify_usd == OPENROUTER_EST_COST_PER_VERIFY_USD

    def test_explicit_cost_always_wins(self, tmp_path: Path) -> None:
        assert (
            Settings(cost_per_verify_usd=0.02, _env_file=None).cost_per_verify_usd
            == 0.02
        )
        env_file = tmp_path / ".env"
        env_file.write_text("COST_PER_VERIFY_USD=0.03\n", encoding="utf-8")
        assert Settings(_env_file=str(env_file)).cost_per_verify_usd == 0.03


class TestAnthropicSettings:
    def test_defaults(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.anthropic_gate_reasoning_effort == "none"
        assert settings.anthropic_verify_reasoning_effort == "low"
        assert settings.anthropic_web_max_uses == 2
        assert settings.anthropic_web_search_tool == "basic"

    @pytest.mark.parametrize("uses", [0, 6])
    def test_max_uses_is_bounded(self, uses: int) -> None:
        with pytest.raises(ValidationError):
            Settings(anthropic_web_max_uses=uses, _env_file=None)

    def test_unknown_effort_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(anthropic_verify_reasoning_effort="extreme", _env_file=None)

    def test_jev_is_kept_out_of_anthropic_model_slots(self) -> None:
        with pytest.raises(ValidationError, match="Jev"):
            Settings(anthropic_gate_model="typesafe/jev-1.13", _env_file=None)


class TestSttResilienceSettings:
    """Defaults and bounds for the STT supervisor knobs (app/stt_supervisor.py)."""

    def test_defaults(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.stt_warm_up is True
        assert settings.stt_failure_threshold == 3
        assert settings.stt_cpu_fallback is True
        assert settings.session_stats_flush_s == 60.0

    def test_env_overrides_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STT_WARM_UP", "false")
        monkeypatch.setenv("STT_FAILURE_THRESHOLD", "5")
        monkeypatch.setenv("STT_CPU_FALLBACK", "false")
        monkeypatch.setenv("SESSION_STATS_FLUSH_S", "15")
        settings = Settings(_env_file=None)
        assert settings.stt_warm_up is False
        assert settings.stt_failure_threshold == 5
        assert settings.stt_cpu_fallback is False
        assert settings.session_stats_flush_s == 15.0

    @pytest.mark.parametrize("threshold", [0, -1])
    def test_threshold_below_one_is_rejected(self, threshold: int) -> None:
        with pytest.raises(ValueError):
            Settings(stt_failure_threshold=threshold, _env_file=None)

    def test_flush_interval_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            Settings(session_stats_flush_s=0, _env_file=None)


class TestSttSegmentation:
    @pytest.mark.parametrize(
        ("backend", "segmentation", "resolved"),
        [
            ("faster-whisper", "auto", "window"),
            ("torch", "auto", "window"),
            ("faster-whisper", "vad", "vad"),
            ("torch", "window", "window"),
        ],
    )
    def test_resolution(self, backend: str, segmentation: str, resolved: str) -> None:
        settings = Settings(
            _env_file=None, stt_backend=backend, stt_segmentation=segmentation
        )
        assert settings.resolved_stt_segmentation == resolved

    def test_warm_up_budget_follows_the_mode(self) -> None:
        assert Settings(_env_file=None, stt_hop_s=3.5).stt_warm_up_budget_s == 3.5
        vad = Settings(
            _env_file=None, stt_segmentation="vad", stt_vad_max_segment_s=8.0
        )
        assert vad.stt_warm_up_budget_s == 4.0

    def test_max_segment_must_fit_under_the_high_watermark(self) -> None:
        with pytest.raises(ValueError, match="STT_VAD_MAX_SEGMENT_S"):
            Settings(
                _env_file=None,
                stt_segmentation="vad",
                stt_vad_max_segment_s=11.5,
                audio_high_watermark_s=12.0,
            )

    def test_min_silence_must_exceed_the_pad(self) -> None:
        with pytest.raises(ValueError, match="STT_VAD_MIN_SILENCE_MS"):
            Settings(_env_file=None, stt_segmentation="vad", stt_vad_min_silence_ms=150)

    def test_vad_rules_do_not_apply_in_window_mode(self) -> None:
        settings = Settings(
            _env_file=None,
            stt_vad_max_segment_s=20.0,
            audio_high_watermark_s=1.0,
            audio_low_watermark_s=0.5,
        )
        assert settings.resolved_stt_segmentation == "window"
