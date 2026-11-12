"""Opt-in, append-only experiment events shared by foreground and workers."""

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_lock = threading.RLock()
_path: Path | None = None
_run_id = ""
_sequence = 0


def configure_experiment_log(path: Path, **metadata: Any) -> None:
    global _path, _run_id, _sequence
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8"):
            pass
        _path = path
        _run_id = uuid.uuid4().hex
        _sequence = 0
        record_event("run_started", **metadata)


def experiment_log_status() -> tuple[Path | None, str]:
    with _lock:
        return _path, _run_id


def stop_experiment_log() -> None:
    global _path
    with _lock:
        record_event("run_stopped")
        _path = None


def record_event(event: str, **data: Any) -> None:
    global _sequence
    with _lock:
        if _path is None:
            return
        _sequence += 1
        payload = {
            "schema_version": 1,
            "run_id": _run_id,
            "sequence": _sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        with _path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


def logged_completion(client: Any, *, component: str, **kwargs: Any) -> Any:
    if _path is None:
        return client.chat.completions.create(**kwargs)
    request_id = uuid.uuid4().hex
    record_event("model_request", request_id=request_id,
                 component=component, request=kwargs)
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(**kwargs)
    except BaseException as exc:
        record_event("model_error", request_id=request_id,
                     component=component, error_type=type(exc).__name__,
                     error=str(exc), elapsed_seconds=time.perf_counter() - started)
        raise
    record_event("model_response", request_id=request_id,
                 component=component, response=response.model_dump(mode="json"),
                 elapsed_seconds=time.perf_counter() - started)
    return response
