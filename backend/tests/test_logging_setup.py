"""Terminal logging: formatting, color policy, and session correlation."""

import logging

import pytest

from app.logging_setup import (
    SessionIdFilter,
    TerminalFormatter,
    banner,
    configure_logging,
    session_id_var,
)


def make_record(
    name: str = "app.pipeline", level: int = logging.INFO, msg: str = "hello %s"
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=("world",), exc_info=None,
    )


class TestTerminalFormatter:
    def test_plain_output_has_no_ansi(self) -> None:
        record = make_record()
        SessionIdFilter().filter(record)
        line = TerminalFormatter(use_color=False).format(record)
        assert "\033[" not in line
        assert "INF" in line and "hello world" in line

    def test_module_prefix_is_stripped(self) -> None:
        record = make_record(name="app.pipeline")
        SessionIdFilter().filter(record)
        line = TerminalFormatter(use_color=False).format(record)
        assert "pipeline" in line
        assert "app.pipeline" not in line

    def test_uvicorn_loggers_are_collapsed(self) -> None:
        record = make_record(name="uvicorn.error")
        SessionIdFilter().filter(record)
        assert "uvicorn" in TerminalFormatter(use_color=False).format(record)

    def test_color_output_wraps_the_level(self) -> None:
        record = make_record()
        SessionIdFilter().filter(record)
        assert "\033[" in TerminalFormatter(use_color=True).format(record)

    def test_exceptions_are_appended(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "app.db", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
            )
        SessionIdFilter().filter(record)
        line = TerminalFormatter(use_color=False).format(record)
        assert "ValueError: boom" in line


class TestSessionCorrelation:
    def test_session_id_is_stamped_and_truncated(self) -> None:
        token = session_id_var.set("abcdef0123456789" * 2)
        try:
            record = make_record()
            SessionIdFilter().filter(record)
            line = TerminalFormatter(use_color=False).format(record)
        finally:
            session_id_var.reset(token)
        assert "abcdef01" in line  # short correlation tag
        assert "abcdef0123456789" not in line  # not the whole uuid

    def test_no_session_means_no_tag(self) -> None:
        record = make_record()
        SessionIdFilter().filter(record)
        line = TerminalFormatter(use_color=False).format(record)
        assert line.count("  ") >= 1
        assert "pipeline " not in line.split("  ")[0]


class TestConfigureLogging:
    def test_installs_even_when_a_handler_exists(self) -> None:
        """basicConfig is a no-op with an existing handler — the exact case
        under pytest, which is where LOG_LEVEL was silently ignored."""
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        original_level = root.level
        try:
            root.handlers = [logging.NullHandler()]
            configure_logging("WARNING")
            assert root.level == logging.WARNING
            assert any(
                isinstance(handler.formatter, TerminalFormatter)
                for handler in root.handlers
            )
        finally:
            root.handlers = original_handlers
            root.setLevel(original_level)

    def test_third_party_loggers_are_quieted_unless_debug(self) -> None:
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        original_level = root.level
        try:
            configure_logging("INFO")
            assert logging.getLogger("httpx").level == logging.WARNING
        finally:
            root.handlers = original_handlers
            root.setLevel(original_level)
            logging.getLogger("httpx").setLevel(logging.NOTSET)


class TestBanner:
    def test_aligns_keys_and_includes_values(self) -> None:
        text = banner([("listening", "http://x"), ("speech", "faster-whisper")], "T")
        assert "T" in text
        assert "listening" in text and "http://x" in text
        assert "speech" in text and "faster-whisper" in text

    def test_empty_rows_do_not_crash(self) -> None:
        assert "Title" in banner([], "Title")
