from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchState(StrEnum):
    DRAFT = "DRAFT"
    SEARCHING = "SEARCHING"
    EVIDENCE_READY = "EVIDENCE_READY"
    HYPOTHESES_READY = "HYPOTHESES_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_DEPENDENCY_APPROVAL = "AWAITING_DEPENDENCY_APPROVAL"
    IMPLEMENTING = "IMPLEMENTING"
    RUNNING = "RUNNING"
    ANALYZING = "ANALYZING"
    BASELINING = "BASELINING"
    ITERATING = "ITERATING"
    REVIEWING = "REVIEWING"
    REPORT_READY = "REPORT_READY"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class Artifact:
    kind: str
    payload: dict[str, Any]
    producer: str
    inputs: list[str] = field(default_factory=list)
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = "0.1"
    created_at: str = field(default_factory=utc_now)
    content_hash: str = ""
    status: str = "created"

    def __post_init__(self) -> None:
        if not self.content_hash:
            content = json.dumps(self.payload, sort_keys=True, ensure_ascii=True).encode()
            object.__setattr__(self, "content_hash", hashlib.sha256(content).hexdigest())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchTask:
    question: str
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=utc_now)
    state: ResearchState = ResearchState.DRAFT
    artifacts: list[str] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    # The controller supplies the profile on every execution; only its digest
    # is persisted because a profile may contain credentials or commands.
    execution_profile_hash: str | None = None
    execution_status: str = "not_queued"
    job_id: str | None = None
    attempt: int = 0
    max_attempts: int = 1
    cancel_requested: bool = False
    pause_requested: bool = False
    paused_from_state: ResearchState | None = None
    iteration: int = 0
    max_iterations: int = 1
    replicates: int = 1
    objective_metric: str = "score"
    objective_direction: str = "max"
    best_iteration: int | None = None
    best_value: float | None = None
    baseline_requested: bool = False
    baseline_artifact_id: str | None = None
    mission_dir: str | None = None
    runtime: dict[str, Any] = field(default_factory=dict)

    def transition(self, target: ResearchState) -> None:
        allowed = {
            ResearchState.DRAFT: {ResearchState.SEARCHING, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.SEARCHING: {ResearchState.EVIDENCE_READY, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.EVIDENCE_READY: {ResearchState.HYPOTHESES_READY, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.HYPOTHESES_READY: {ResearchState.AWAITING_APPROVAL, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.AWAITING_APPROVAL: {ResearchState.BASELINING, ResearchState.IMPLEMENTING, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.BASELINING: {ResearchState.IMPLEMENTING, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.IMPLEMENTING: {ResearchState.RUNNING, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.RUNNING: {ResearchState.ANALYZING, ResearchState.AWAITING_DEPENDENCY_APPROVAL, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.ANALYZING: {ResearchState.ITERATING, ResearchState.REVIEWING, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.ITERATING: {ResearchState.IMPLEMENTING, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.REVIEWING: {ResearchState.REPORT_READY, ResearchState.CANCELLED, ResearchState.FAILED},
            ResearchState.REPORT_READY: set(),
            ResearchState.CANCELLED: set(),
            ResearchState.FAILED: {ResearchState.DRAFT, ResearchState.SEARCHING, ResearchState.IMPLEMENTING},
            ResearchState.PAUSED: {ResearchState.DRAFT, ResearchState.SEARCHING, ResearchState.EVIDENCE_READY, ResearchState.HYPOTHESES_READY, ResearchState.AWAITING_APPROVAL, ResearchState.AWAITING_DEPENDENCY_APPROVAL, ResearchState.IMPLEMENTING, ResearchState.RUNNING, ResearchState.ANALYZING, ResearchState.REVIEWING, ResearchState.ITERATING, ResearchState.BASELINING, ResearchState.CANCELLED},
            ResearchState.AWAITING_DEPENDENCY_APPROVAL: {ResearchState.IMPLEMENTING, ResearchState.PAUSED, ResearchState.CANCELLED, ResearchState.FAILED},
        }
        if target not in allowed[self.state]:
            raise ValueError(f"invalid transition: {self.state} -> {target}")
        self.history.append({"from": self.state.value, "to": target.value, "at": utc_now()})
        self.state = target
        if target != ResearchState.FAILED:
            self.error = None

    def reset_for_retry(self, resume_from: "ResearchState" = ResearchState.DRAFT) -> None:
        """Start a clean workflow attempt while preserving failure history."""
        if self.state != ResearchState.FAILED:
            raise ValueError("only failed tasks can be retried")
        if resume_from not in {ResearchState.DRAFT, ResearchState.IMPLEMENTING}:
            raise ValueError("retry can resume only from DRAFT or IMPLEMENTING")
        self.transition(resume_from)
        self.error = None
        self.iteration = 0
        self.best_iteration = None
        self.best_value = None
        self.baseline_artifact_id = None

    def prepare_orphaned_retry(self) -> None:
        """Convert a recovered orphan into a fresh, explicitly retryable task."""
        if self.execution_status != "orphaned":
            raise ValueError("only orphaned tasks can be resubmitted")
        if self.state in {ResearchState.REPORT_READY, ResearchState.CANCELLED}:
            raise ValueError(f"terminal task cannot be resubmitted from state {self.state.value}")
        self.state = ResearchState.FAILED
        self.error = "orphaned worker execution will be retried"
        self.execution_status = "not_queued"
        self.cancel_requested = False
        self.reset_for_retry()
