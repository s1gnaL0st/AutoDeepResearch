from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .models import Artifact, ResearchTask
from .protocol import A2AMessage


Validator = Callable[[Any], tuple[dict[str, Any] | None, str | None]]


def _analysis_result(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "response must be a JSON object"
    finding = value.get("finding")
    confidence = value.get("confidence", "descriptive_only")
    if not isinstance(finding, str) or not finding.strip():
        return None, "finding must be a non-empty string"
    if confidence not in {"descriptive_only", "hypothesis_only", "human_review_required"}:
        return None, "confidence must be descriptive_only, hypothesis_only or human_review_required"
    result: dict[str, Any] = {"finding": finding.strip(), "confidence": confidence}
    for key in ("effect", "delta_vs_baseline"):
        if key in value:
            if key == "effect" and not isinstance(value[key], (int, float)):
                return None, "effect must be numeric"
            if key == "delta_vs_baseline" and not isinstance(value[key], Mapping):
                return None, "delta_vs_baseline must be an object"
            result[key] = value[key]
    for key in ("metrics", "statistics", "limitations", "claim_evidence"):
        if key in value:
            if key in {"metrics", "statistics", "claim_evidence"} and not isinstance(value[key], Mapping):
                return None, f"{key} must be an object"
            if key == "limitations" and (not isinstance(value[key], list) or not all(isinstance(item, str) for item in value[key])):
                return None, "limitations must be a string array"
            result[key] = value[key]
    return result, None


def _review_result(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "response must be a JSON object"
    decision = value.get("decision")
    if decision not in {"requires_human_review", "pass_with_human_review", "blocked"}:
        return None, "decision must be requires_human_review, pass_with_human_review or blocked"
    result: dict[str, Any] = {"decision": decision}
    for key in ("blocking_issues", "scientific_limitations", "reproducibility"):
        values = value.get(key, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            return None, f"{key} must be a string array"
        result[key] = values
    return result, None


class SubprocessJsonAgent:
    """Shared non-shell runner for strict Analysis/Reviewer JSON adapters."""

    kind: str
    name: str
    capabilities: tuple[str, ...]
    validator: Validator

    def __init__(self, command: Sequence[str], cwd: str | os.PathLike[str], timeout_seconds: int = 900) -> None:
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError(f"{self.name} command must be a non-empty string array")
        self.command = tuple(command)
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"{self.name} cwd does not exist: {self.cwd}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        request = {
            "task_id": message.task_id,
            "action": message.action,
            "question": task.question,
            "input_artifacts": message.input_artifacts,
            "input_artifact_data": message.input_artifact_data,
            "parameters": message.parameters,
        }
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command, cwd=str(self.cwd), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate((json.dumps(request, ensure_ascii=True) + "\n").encode("utf-8")),
                timeout=self.timeout_seconds,
            )
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            try:
                parsed = json.loads(stdout_text.strip())
                normalized, validation_error = self.validator(parsed)
            except json.JSONDecodeError as exc:
                parsed = None
                normalized = None
                validation_error = f"invalid JSON: {exc}"
            status = "created" if process.returncode == 0 and normalized is not None else "failed"
            payload: dict[str, Any] = {
                "provider": "subprocess",
                "command": list(self.command),
                "cwd": str(self.cwd),
                "returncode": process.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "request": request,
            }
            if normalized is not None:
                payload["result"] = normalized
                # Keep the canonical Artifact schema at the top level while
                # retaining the normalized response for audit/debugging.
                payload.update(normalized)
            if validation_error:
                payload["validation_error"] = validation_error
            return Artifact(kind=self.kind, producer=self.name, inputs=message.input_artifacts, status=status, payload=payload)
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return Artifact(kind=self.kind, producer=self.name, inputs=message.input_artifacts, status="failed", payload={
                "provider": "subprocess", "command": list(self.command), "cwd": str(self.cwd),
                "error": "timeout", "timeout_seconds": self.timeout_seconds, "request": request,
            })
        except (OSError, UnicodeError) as exc:
            return Artifact(kind=self.kind, producer=self.name, inputs=message.input_artifacts, status="failed", payload={
                "provider": "subprocess", "command": list(self.command), "cwd": str(self.cwd),
                "error": str(exc), "request": request,
            })


class SubprocessAnalysisAgent(SubprocessJsonAgent):
    kind = "Finding"
    name = "analysis"
    capabilities = ("analyze_results", "map_claims_to_evidence")
    validator = staticmethod(_analysis_result)


class SubprocessReviewerAgent(SubprocessJsonAgent):
    kind = "ReviewReport"
    name = "reviewer"
    capabilities = ("review", "attempt_falsification")
    validator = staticmethod(_review_result)

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        artifact = await super().handle(message, task)
        result = artifact.payload.get("result", {})
        if artifact.status == "created" and result.get("decision") == "blocked":
            return Artifact(kind=artifact.kind, producer=artifact.producer, inputs=artifact.inputs, status="failed", payload=artifact.payload)
        return artifact
