"""Redacted Uvicorn logs loaded through Freqtrade's supported log_config option."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
import traceback


_URL = re.compile(r"(?:https?://[^\s\"'<>]+|/[^\s\"'<>]+)")
_AUTHORIZATION = re.compile(
    r"(?i)(?:b)?[\"']?authorization[\"']?\s*(?::|=|,)\s*[^\r\n]*"
)
_METHOD = re.compile(r"^[A-Z]{1,16}$")


def _path_without_query(value: object) -> str:
    # Uvicorn supplies a request target, including the complete query string.
    return str(value).split("?", 1)[0].split("#", 1)[0]


def _redact_message(value: str) -> str:
    value = _URL.sub(lambda match: _path_without_query(match.group()), value)
    # Includes textual headers, JSON/dict representations and bytes header tuples.
    # Discard the remainder of an Authorization line rather than guess token lengths.
    return _AUTHORIZATION.sub("Authorization: [REDACTED]", value)


class AccessFieldsFilter(logging.Filter):
    """Allow only Uvicorn's five documented HTTP access fields to reach storage."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.args, tuple) or len(record.args) != 5:
            # Unknown record shapes must not fall back to rendering raw messages.
            return False
        client, method, target, _http_version, status = record.args
        method = str(method)
        if not _METHOD.fullmatch(method) or not isinstance(status, int):
            return False
        record.access_fields = {
            "time_utc": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "method": method,
            "path": _path_without_query(target),
            "client": _redact_message(str(client)),
            "status": status,
        }
        record.msg = ""
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class AccessJsonFormatter(logging.Formatter):
    """No message, headers, request body or arbitrary LogRecord extras are emitted."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(record.access_fields, ensure_ascii=True, separators=(",", ":"))


class UvicornRedactionFilter(logging.Filter):
    """Redact WS query tokens before uvicorn.error propagates to FT root handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = "Uvicorn diagnostic omitted: invalid log record"
        if record.exc_info:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info))
        if record.stack_info:
            message += "\n" + record.stack_info
        record.msg = _redact_message(message)
        record.args = ()
        # A downstream formatter must never render the original traceback again.
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True
