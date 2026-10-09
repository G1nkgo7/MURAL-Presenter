"""Opt-in operator telemetry, separate from conversation and training traces.

The destination must be a node-local file outside the per-query trace. O_APPEND
and one bounded write support writers on the same host, not network filesystems.
No directories are created and telemetry failures never interrupt model calls.
"""

import json
import math
import os
from pathlib import Path
import time
from collections.abc import Mapping


EVENTS = frozenset({
    "provider_attempt_started", "provider_attempt_succeeded",
    "provider_attempt_failed", "provider_failed",
    "resource_wait_completed", "resource_wait_failed",
})
TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
STRING_FIELDS = (
    "attempt_id", "channel_id", "model", "request_kind", "error_kind", "resource",
)
NUMBER_FIELDS = ("status_code", "elapsed_s", "first_byte_s", "queue_wait_s")


def _number(value):
    return (type(value) in (int, float) and math.isfinite(value)
            and value >= 0)


def append_operator_metrics(path, event_type, fields, *, trace_root=None):
    """Append only allowlisted scalar metadata; silently drop unsafe records."""
    if not path or event_type not in EVENTS:
        return
    try:
        destination = Path(path)
        if not destination.is_absolute():
            return
        destination = destination.resolve()
        if trace_root and destination.is_relative_to(Path(trace_root).resolve()):
            return
        record = {"ts": float(time.time()), "event": event_type}
        for name in STRING_FIELDS:
            value = fields.get(name)
            if type(value) is str and len(value) <= 256:
                record[name] = value
        effort = fields.get("effort", fields.get("effective_effort"))
        if type(effort) is str and len(effort) <= 256:
            record["effort"] = effort
        for name in NUMBER_FIELDS:
            value = fields.get(name)
            if _number(value):
                record[name] = value
        if type(fields.get("retryable")) is bool:
            record["retryable"] = fields["retryable"]
        usage = {}
        source = fields.get("usage")
        if isinstance(source, Mapping):
            for name in TOKEN_FIELDS:
                value = source.get(name)
                if type(value) is int and value >= 0:
                    usage[name] = value
        # Older success events may expose only the output counter in diagnostics.
        diagnostics = fields.get("response_diagnostics")
        if event_type == "provider_attempt_succeeded" and isinstance(diagnostics, Mapping):
            value = diagnostics.get("output_tokens")
            if "output_tokens" not in usage and type(value) is int and value >= 0:
                usage["output_tokens"] = value
        if usage:
            record["usage"] = usage
        data = (json.dumps(record, ensure_ascii=True, allow_nan=False,
                           separators=(",", ":")) + "\n").encode("ascii")
        if len(data) > 2048:
            return
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except Exception:
        return
