import json
import logging
import sys

import pytest
import structlog

from app.api.middleware import resolve_request_id
from app.core.logging import REDACTED, configure_logging, redact_sensitive


@pytest.fixture(autouse=True)
def restore_logging() -> None:
    yield
    structlog.contextvars.clear_contextvars()
    logging.getLogger().handlers.clear()


def test_redact_sensitive_masks_known_keys_case_insensitively() -> None:
    event = redact_sensitive(
        None, "info", {"event": "x", "Password": "p", "email": "a@b.c", "user_count": 3}
    )

    assert event == {"event": "x", "Password": REDACTED, "email": REDACTED, "user_count": 3}


def test_json_logs_carry_context_and_redact_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    structlog.contextvars.bind_contextvars(request_id="req-1")

    structlog.stdlib.get_logger("test").info("something_happened", token="abc", items=2)

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "something_happened"
    assert line["request_id"] == "req-1"
    assert line["token"] == REDACTED
    assert line["items"] == 2
    assert line["level"] == "info"
    assert line["timestamp"].endswith("Z")


def test_stdlib_loggers_are_rendered_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")

    logging.getLogger("uvicorn.error").info("Started server process")

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "Started server process"
    assert line["logger"] == "uvicorn.error"


def test_level_filters_and_uvicorn_access_log_is_disabled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("WARNING", "json")

    structlog.stdlib.get_logger("test").info("too_quiet")
    logging.getLogger("uvicorn.access").warning("GET / 200")

    assert capsys.readouterr().out == ""


def test_exceptions_are_logged_as_structured_tracebacks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", "json")

    try:
        raise ValueError("boom")
    except ValueError:
        structlog.stdlib.get_logger("test").exception("failed")

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["exception"][0]["exc_type"] == "ValueError"


@pytest.mark.parametrize(
    ("incoming", "reused"),
    [
        ("abc-123", True),
        ("a.b_c-D", True),
        ("x" * 128, True),
        ("x" * 129, False),
        ("has space", False),
        ("new\nline", False),
        ("", False),
        (None, False),
    ],
)
def test_resolve_request_id(incoming: str | None, reused: bool) -> None:
    resolved = resolve_request_id(incoming)

    assert (resolved == incoming) is reused
    assert resolved


def test_logs_can_go_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json", stream=sys.stderr)

    structlog.stdlib.get_logger("test").info("to_stderr")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.strip().splitlines()[-1])["event"] == "to_stderr"
