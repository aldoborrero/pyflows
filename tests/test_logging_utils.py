import json
import logging

from pyflows.logging_utils import JsonFormatter, TextFormatter


def test_json_formatter_emits_json() -> None:
    record = logging.LogRecord(
        name="pyflows.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["logger"] == "pyflows.test"
    assert payload["level"] == "INFO"
    assert payload["message"] == "hello world"
    assert "ts" in payload


def test_text_formatter_emits_message() -> None:
    record = logging.LogRecord(
        name="pyflows.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="warn",
        args=(),
        exc_info=None,
    )
    formatted = TextFormatter().format(record)
    assert "WARNING" in formatted
    assert "pyflows.test" in formatted
    assert "warn" in formatted
