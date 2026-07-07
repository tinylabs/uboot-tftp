"""Process-local session tracking for resumable user handlers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from collections.abc import Coroutine
from typing import Literal

from .protocol import ParsedPath, normalize_client_id

SessionPhase = Literal["await_rrq", "await_upload", "complete"]


@dataclass
class PendingReceive:
    token: str
    upload_path: str
    size: int
    uploaded: bytes | None = None


@dataclass
class ClientSession:
    client_id: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    rrq_count: int = 0
    last_path: str = "/"
    requests: list[ParsedPath] = field(default_factory=list)
    phase: SessionPhase = "await_rrq"
    handler: Coroutine[object, bytes | None, object] | None = None
    pending_receive: PendingReceive | None = None
    server_ip: str = "127.0.0.1"
    current_token: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    public_env: dict[str, str] = field(default_factory=dict)
    is_le: bool | None = None
    preflight_pending: bool = False
    download_artifacts: set[str] = field(default_factory=set)

    def record_rrq(self, parsed: ParsedPath) -> None:
        self.updated_at = time.time()
        self.rrq_count += 1
        self.last_path = parsed.path
        self.requests.append(parsed)


class InMemorySessionStore:
    """Simple process-local session store keyed by client identifier."""

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}

    def get(self, client_id: str) -> ClientSession | None:
        return self._sessions.get(normalize_client_id(client_id))

    def create(self, client_id: str) -> ClientSession:
        session = ClientSession(client_id=normalize_client_id(client_id))
        self._sessions[session.client_id] = session
        return session

    def get_or_create(self, client_id: str) -> ClientSession:
        return self.get(client_id) or self.create(client_id)

    def replace(self, client_id: str) -> ClientSession:
        normalized = normalize_client_id(client_id)
        self._sessions.pop(normalized, None)
        return self.create(normalized)

    def require(self, client_id: str) -> ClientSession:
        session = self.get(client_id)
        if session is None:
            raise FileNotFoundError(f"no session for client id {client_id!r}")
        return session

    def discard(self, client_id: str) -> None:
        self._sessions.pop(normalize_client_id(client_id), None)

    def all(self) -> dict[str, ClientSession]:
        return dict(self._sessions)
