from __future__ import annotations

from typing import Any

from .models import ResearchTask
from .storage import ArtifactStore


def research_status(task: ResearchTask, store: ArtifactStore) -> dict[str, Any]:
    """Build a read-only task summary without replacing source Artifacts."""
    records = [store.get_artifact(artifact_id) for artifact_id in task.artifacts]

    def latest(kind: str) -> dict[str, Any] | None:
        return next((record for record in reversed(records) if record.get("kind") == kind), None)

    evidence = latest("EvidenceSet")
    finding = latest("Finding")
    review = latest("ReviewReport")
    report = latest("ResearchReport")
    package = latest("ReproducibilityPackage")
    failure = next(
        (record for record in reversed(records) if record.get("status") in {"failed", "insufficient_evidence"}),
        None,
    )
    return {
        "task_id": task.task_id,
        "question": task.question,
        "state": task.state.value,
        "execution_status": task.execution_status,
        "cancel_requested": task.cancel_requested,
        "error": task.error,
        "runtime": task.runtime,
        "artifact_count": len(records),
        "iteration": {
            "current": task.iteration,
            "maximum": task.max_iterations,
            "replicates": task.replicates,
            "objective_metric": task.objective_metric,
            "objective_direction": task.objective_direction,
            "best_iteration": task.best_iteration,
            "best_value": task.best_value,
        },
        "evidence": {
            "artifact_id": evidence.get("artifact_id") if evidence else None,
            "summary": evidence.get("payload", {}).get("summary") if evidence else None,
        },
        "finding": {
            "artifact_id": finding.get("artifact_id") if finding else None,
            "text": finding.get("payload", {}).get("finding") if finding else None,
            "confidence": finding.get("payload", {}).get("confidence") if finding else None,
        },
        "review": {
            "artifact_id": review.get("artifact_id") if review else None,
            "decision": review.get("payload", {}).get("decision") if review else None,
        },
        "report": {
            "artifact_id": report.get("artifact_id") if report else None,
            "status": report.get("payload", {}).get("report_status") if report else None,
        },
        "reproducibility_artifact_id": package.get("artifact_id") if package else None,
        "latest_failure": {
            "artifact_id": failure.get("artifact_id") if failure else None,
            "kind": failure.get("kind") if failure else None,
            "status": failure.get("status") if failure else None,
            "error": failure.get("payload", {}).get("error") if failure else None,
        },
    }
