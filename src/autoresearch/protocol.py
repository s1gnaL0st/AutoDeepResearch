from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import Artifact, ResearchTask


@dataclass(frozen=True)
class A2AMessage:
    """Transport-neutral A2A-like envelope. Network adapters can serialize this directly."""

    task_id: str
    sender: str
    recipient: str
    action: str
    input_artifacts: list[str] = field(default_factory=list)
    input_artifact_data: list[dict[str, Any]] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)


class ResearchAgent(Protocol):
    name: str
    capabilities: tuple[str, ...]

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact: ...
