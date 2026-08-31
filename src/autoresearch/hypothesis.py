from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import Artifact, ResearchTask
from .protocol import A2AMessage


def _validate_hypotheses(value: Any) -> list[dict[str, Any]] | None:
    values = value.get("hypotheses") if isinstance(value, Mapping) else value
    if not isinstance(values, list) or not values:
        return None
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping):
            return None
        identifier = item.get("id")
        statement = item.get("statement")
        if not isinstance(identifier, str) or not identifier.strip():
            return None
        identifier = identifier.strip()
        if identifier in seen:
            return None
        if not isinstance(statement, str) or not statement.strip():
            return None
        normalized_item: dict[str, Any] = {"id": identifier, "statement": statement.strip()}
        for key in ("metric", "direction", "rationale", "test_criterion"):
            if key in item:
                if not isinstance(item[key], str):
                    return None
                normalized_item[key] = item[key].strip()
        normalized.append(normalized_item)
        seen.add(identifier)
    return normalized


class SubprocessHypothesisAgent:
    """Run a structured hypothesis generator as an isolated external command."""

    name = "hypothesis"
    capabilities = ("generate_hypotheses", "revise_hypotheses", "propose_tests")

    def __init__(self, command: Sequence[str], cwd: str | os.PathLike[str], timeout_seconds: int = 900) -> None:
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("hypothesis command must be a non-empty string array")
        self.command = tuple(command)
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"hypothesis cwd does not exist: {self.cwd}")
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
        stdout_text = ""
        stderr_text = ""
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(self.cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate((json.dumps(request, ensure_ascii=True) + "\n").encode("utf-8")),
                timeout=self.timeout_seconds,
            )
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            parsed: Any = None
            parse_error = None
            try:
                parsed = json.loads(stdout_text.strip())
            except json.JSONDecodeError as exc:
                parse_error = str(exc)
            hypotheses = _validate_hypotheses(parsed)
            status = "created" if process.returncode == 0 and hypotheses is not None else "failed"
            payload: dict[str, Any] = {
                "provider": "subprocess",
                "command": list(self.command),
                "cwd": str(self.cwd),
                "returncode": process.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "request": request,
            }
            if hypotheses is not None:
                payload["hypotheses"] = hypotheses
            if parse_error:
                payload["parse_error"] = parse_error
            elif parsed is not None and hypotheses is None:
                payload["validation_error"] = "response must contain a non-empty unique hypotheses array with id and statement"
            return Artifact(kind="HypothesisSet", producer=self.name, inputs=message.input_artifacts, status=status, payload=payload)
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return Artifact(kind="HypothesisSet", producer=self.name, inputs=message.input_artifacts, status="failed", payload={
                "provider": "subprocess", "command": list(self.command), "cwd": str(self.cwd),
                "error": "timeout", "timeout_seconds": self.timeout_seconds, "request": request,
            })
        except (OSError, UnicodeError) as exc:
            return Artifact(kind="HypothesisSet", producer=self.name, inputs=message.input_artifacts, status="failed", payload={
                "provider": "subprocess", "command": list(self.command), "cwd": str(self.cwd),
                "error": str(exc), "request": request,
            })
