# pipeline_registry.py
"""session_id -> live PipelineWorker + LLMContext, for as long as a call is
connected. Same pattern as kyc-voice-agent's pipeline_registry.py -- lets
server.py/admin.py reach into a running call's state (e.g. to check whether
a session is currently live) without threading it through every function
signature.
"""
from __future__ import annotations

_tasks: dict[str, tuple] = {}  # session_id -> (worker, context)


def register_task(session_id: str, worker, context) -> None:
    _tasks[session_id] = (worker, context)


def unregister_task(session_id: str) -> None:
    _tasks.pop(session_id, None)


def get_task(session_id: str):
    return _tasks.get(session_id)
