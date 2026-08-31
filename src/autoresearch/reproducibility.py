from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import Artifact, ResearchTask
from .storage import ArtifactStore


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def build_reproducibility_package(task: ResearchTask, store: ArtifactStore) -> Artifact:
    """Create a compact manifest that verifies every prior Artifact in a research task."""
    records = [store.get_artifact(artifact_id) for artifact_id in task.artifacts]
    known_ids = {record["artifact_id"] for record in records}
    manifest = []
    integrity_errors: list[str] = []
    for record in records:
        expected_hash = _payload_hash(record["payload"])
        if expected_hash != record["content_hash"]:
            integrity_errors.append(f"{record['artifact_id']}: content hash mismatch")
        missing_inputs = [artifact_id for artifact_id in record["inputs"] if artifact_id not in known_ids]
        if missing_inputs:
            integrity_errors.append(f"{record['artifact_id']}: unknown inputs {missing_inputs}")
        manifest.append({
            "artifact_id": record["artifact_id"], "kind": record["kind"], "producer": record["producer"],
            "status": record["status"], "content_hash": record["content_hash"], "inputs": record["inputs"],
        })

    # Retried tasks retain prior attempt Artifacts. The reproducibility package
    # describes the latest completed research attempt, while its manifest still
    # carries every historical Artifact for audit.
    evidence = next((record for record in reversed(records) if record["kind"] == "EvidenceSet"), None)
    code_revisions = [record for record in records if record["kind"] == "CodeRevision"]
    runs = [record for record in records if record["kind"] == "ExperimentRun"]
    findings = [record for record in records if record["kind"] == "Finding"]
    claim_maps = [record for record in records if record["kind"] == "ClaimEvidenceMap"]
    adjudications = [record for record in records if record["kind"] == "EvidenceAdjudication"]
    run = runs[-1] if runs else None
    code_revision = code_revisions[-1] if code_revisions else None
    finding = findings[-1] if findings else None
    claim_map = claim_maps[-1] if claim_maps else None
    adjudication = adjudications[-1] if adjudications else None
    review = next((record for record in reversed(records) if record["kind"] == "ReviewReport"), None)
    replay = None
    if run and run["payload"].get("executor") in {"local", "docker"}:
        replay = {"executor": run["payload"]["executor"], "command": run["payload"].get("command"), "cwd": run["payload"].get("cwd")}

    status = "validated" if not integrity_errors else "integrity_failed"
    return Artifact(kind="ReproducibilityPackage", producer="control-plane", inputs=list(task.artifacts), status=status, payload={
        "task_id": task.task_id,
        "question": task.question,
        "task_state": task.state.value,
        "objective": {
            "metric": task.objective_metric, "direction": task.objective_direction,
            "best_iteration": task.best_iteration, "best_value": task.best_value,
        },
        "artifact_manifest": manifest,
        "integrity_errors": integrity_errors,
        "evidence_summary": evidence["payload"].get("summary") if evidence else None,
        "code_provenance": {
            "artifact_id": code_revision["artifact_id"] if code_revision else None,
            "producer": code_revision["producer"] if code_revision else None,
            "status": code_revision["status"] if code_revision else None,
            "workspace_before": code_revision["payload"].get("workspace_before") if code_revision else None,
            "workspace_after": code_revision["payload"].get("workspace_after") if code_revision else None,
            "workspace_change_detected": code_revision["payload"].get("workspace_change_detected") if code_revision else None,
        },
        "experiment_metrics": finding["payload"].get("metrics") if finding else (run["payload"].get("metrics") if run else None),
        "experiment_statistics": finding["payload"].get("statistics") if finding else None,
        "experiment_trajectory": [{
            "artifact_id": item["artifact_id"], "iteration": item["payload"].get("iteration"),
            "replicate": item["payload"].get("replicate"),
            "status": item["status"], "metrics": item["payload"].get("metrics", {}),
        } for item in runs],
        "claim_evidence_map": {
            "artifact_id": claim_map["artifact_id"] if claim_map else None,
            "summary": claim_map["payload"].get("summary") if claim_map else None,
            "claims": claim_map["payload"].get("claims", []) if claim_map else [],
            "experiment_artifact_id": run["artifact_id"] if run else None,
            "experiment_artifact_ids": finding["payload"].get("replicate_artifact_ids", []) if finding else [],
            "finding_support_level": finding["payload"].get("confidence") if finding else "missing",
            "adjudication_artifact_id": adjudication["artifact_id"] if adjudication else None,
            "adjudication_summary": adjudication["payload"].get("summary") if adjudication else None,
            "adjudications": adjudication["payload"].get("decisions", []) if adjudication else [],
        },
        "review_artifact_id": review["artifact_id"] if review else None,
        "replay": replay,
    })
