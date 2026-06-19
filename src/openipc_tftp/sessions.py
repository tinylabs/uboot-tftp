"""In-memory client session tracking."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock

from .protocol import ClientMessage


@dataclass(frozen=True)
class UBootAction:
    """A queued command for the next script sent to a U-Boot client."""

    kind: str
    name: str
    value: str | None = None
    saveenv: bool = False
    commands: tuple[str, ...] = ()


@dataclass
class ClientSession:
    """Server-side state for one U-Boot client stream."""

    ethaddr: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    sequence: int = 0
    env: dict[str, str] = field(default_factory=dict)
    observed_vars: dict[str, str] = field(default_factory=dict)
    completed_sets: dict[str, str] = field(default_factory=dict)
    reports: dict[str, str] = field(default_factory=dict)
    completed_actions: dict[str, str] = field(default_factory=dict)
    action_queue: list[UBootAction] = field(default_factory=list)
    messages: list[ClientMessage] = field(default_factory=list)

    def record(self, message: ClientMessage) -> None:
        self.updated_at = time.time()
        self.sequence += 1
        self.messages.append(message)
        if message.channel == "env":
            self.env.update(message.values)
        elif message.channel == "var":
            self.observed_vars.update(message.values)
        elif message.channel == "set":
            self.completed_sets.update(message.values)
        elif message.channel == "report":
            self.reports.update(message.values)
        elif message.channel in {
            "run",
            "boot",
            "reset",
            "sleep",
            "printenv",
            "probe",
            "export-env",
        }:
            self.completed_actions.update(message.values)


class InMemorySessionStore:
    """Simple process-local session store keyed by client ethaddr."""

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}
        self._lock = RLock()

    def get_or_create(self, ethaddr: str) -> ClientSession:
        with self._lock:
            session = self._sessions.get(ethaddr)
            if session is None:
                session = ClientSession(ethaddr=ethaddr)
                self._sessions[ethaddr] = session
            return session

    def record(self, message: ClientMessage) -> ClientSession:
        with self._lock:
            session = self.get_or_create(message.ethaddr)
            session.record(message)
            return session

    def queue_action(self, ethaddr: str, action: UBootAction) -> None:
        with self._lock:
            session = self.get_or_create(ethaddr)
            session.action_queue.append(action)

    def next_action(self, ethaddr: str) -> UBootAction | None:
        with self._lock:
            session = self.get_or_create(ethaddr)
            if not session.action_queue:
                return None
            return session.action_queue.pop(0)

    def all(self) -> dict[str, ClientSession]:
        with self._lock:
            return dict(self._sessions)
