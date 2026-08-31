from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .models import Artifact, ResearchTask
from .protocol import A2AMessage


def _workspace_snapshot(cwd: Path) -> dict[str, Any]:
    """Capture compact Git provenance without storing a mutable checkout copy."""
    try:
        inside = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"is_git_repository": False, "error": str(exc)}
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"is_git_repository": False}

    def git_output(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd), *arguments],
                capture_output=True, text=True, timeout=10, check=False,
            )
            return result.stdout if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    head = git_output("rev-parse", "HEAD").strip() or None
    status = git_output("status", "--porcelain=v1", "--untracked-files=all")
    diff = git_output("diff", "--binary", "--no-ext-diff")
    staged_diff = git_output("diff", "--cached", "--binary", "--no-ext-diff")
    return {
        "is_git_repository": True,
        "head": head,
        "status": status,
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "worktree_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "staged_diff_sha256": hashlib.sha256(staged_diff.encode("utf-8")).hexdigest(),
        "dirty": bool(status),
    }


def _workspace_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = ("is_git_repository", "head", "status_sha256", "worktree_diff_sha256", "staged_diff_sha256")
    return any(before.get(key) != after.get(key) for key in keys)


class SubprocessCodingAgent:
    """Run an external coding agent without a shell and retain its raw response."""

    name = "coding"
    capabilities = ("inspect_repo", "implement_experiment", "repair_experiment", "run_tests", "explain_failure")

    def __init__(self, command: Sequence[str], cwd: str | os.PathLike[str], timeout_seconds: int = 900) -> None:
        if not command or not command[0]:
            raise ValueError("coding command must not be empty")
        self.command = tuple(command)
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"coding cwd does not exist: {self.cwd}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        workspace_before = _workspace_snapshot(self.cwd)
        envelope = {
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
                process.communicate((json.dumps(envelope, ensure_ascii=True) + "\n").encode()),
                timeout=self.timeout_seconds,
            )
            workspace_after = _workspace_snapshot(self.cwd)
            return Artifact(kind="CodeRevision", producer=self.name, inputs=message.input_artifacts,
                status="created" if process.returncode == 0 else "failed", payload={
                    "command": list(self.command), "cwd": str(self.cwd), "returncode": process.returncode,
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"), "request": envelope,
                    "workspace_before": workspace_before,
                    "workspace_after": workspace_after,
                    "workspace_change_detected": _workspace_changed(workspace_before, workspace_after),
                })
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            workspace_after = _workspace_snapshot(self.cwd)
            return Artifact(kind="CodeRevision", producer=self.name, inputs=message.input_artifacts,
                status="failed", payload={"command": list(self.command), "cwd": str(self.cwd),
                "error": "timeout", "timeout_seconds": self.timeout_seconds, "request": envelope,
                "workspace_before": workspace_before, "workspace_after": workspace_after,
                "workspace_change_detected": _workspace_changed(workspace_before, workspace_after)})
        except (OSError, UnicodeError) as exc:
            workspace_after = _workspace_snapshot(self.cwd)
            return Artifact(kind="CodeRevision", producer=self.name, inputs=message.input_artifacts,
                status="failed", payload={"command": list(self.command), "cwd": str(self.cwd),
                "error": str(exc), "request": envelope,
                "workspace_before": workspace_before, "workspace_after": workspace_after,
                "workspace_change_detected": _workspace_changed(workspace_before, workspace_after)})


class ClaudeCodeAgent:
    """Adapter for the non-interactive Claude Code CLI.

    Claude Code is intentionally invoked as an argv list. Authentication and
    permission policy remain the operator's responsibility; this adapter never
    adds a bypass-permissions flag implicitly.
    """

    name = "coding"
    capabilities = ("inspect_repo", "implement_experiment", "repair_experiment", "run_tests", "explain_failure")

    def __init__(
        self,
        cwd: str | os.PathLike[str],
        executable: str = "claude",
        timeout_seconds: int = 900,
        extra_args: Sequence[str] = (),
    ) -> None:
        if not executable or any(char in executable for char in "\r\n"):
            raise ValueError("claude executable must be a non-empty single line")
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"coding cwd does not exist: {self.cwd}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if any(not isinstance(arg, str) or not arg for arg in extra_args):
            raise ValueError("extra_args must contain non-empty strings")
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.extra_args = tuple(extra_args)

    def _prompt(self, message: A2AMessage, task: ResearchTask) -> str:
        # Keep the interactive prompt bounded; the complete structured context
        # remains available to subprocess adapters through the JSON envelope.
        context = json.dumps(message.input_artifact_data, ensure_ascii=True, separators=(",", ":"))
        if len(context) > 12000:
            context = context[:12000] + "...<truncated>"
        return "\n".join([
            "You are the coding agent in an AutoResearch workflow.",
            f"Research question: {task.question}",
            f"Requested action: {message.action}",
            f"Input Artifact IDs: {', '.join(message.input_artifacts) or '(none)'}",
            f"Input Artifact context (bounded JSON): {context}",
            "Inspect the repository and perform the requested action. For repair_experiment, use the failed ExperimentRun traceback, command, cwd, and environment to diagnose and fix the experiment entry point, arguments, or dependencies, then run a focused verification.",
            "Do not merely explain a failure: when repair_experiment is requested, modify the workspace so Compute can retry it.",
            "Keep changes reproducible and report what changed. Do not fabricate metrics.",
        ])

    def _command(self, prompt: str) -> list[str]:
        # `-p` is Claude Code's print/non-interactive mode. JSON output keeps
        # the vendor response machine-readable while raw stdout is retained.
        return [self.executable, "-p", prompt, "--output-format", "json", *self.extra_args]

    @staticmethod
    def _parse_output(stdout: str) -> dict[str, Any] | None:
        try:
            value = json.loads(stdout.strip())
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else {"result": value}

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        workspace_before = _workspace_snapshot(self.cwd)
        prompt = self._prompt(message, task)
        command = self._command(prompt)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=str(self.cwd), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            parsed = self._parse_output(stdout_text)
            status = "created" if process.returncode == 0 and parsed is not None else "failed"
            workspace_after = _workspace_snapshot(self.cwd)
            return Artifact(kind="CodeRevision", producer=self.name, inputs=message.input_artifacts, status=status, payload={
                "provider": "claude-code",
                "command": command,
                "cwd": str(self.cwd),
                "returncode": process.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "response": parsed,
                "request": {"task_id": message.task_id, "action": message.action, "question": task.question, "input_artifacts": message.input_artifacts, "input_artifact_data": message.input_artifact_data, "parameters": message.parameters},
                "workspace_before": workspace_before,
                "workspace_after": workspace_after,
                "workspace_change_detected": _workspace_changed(workspace_before, workspace_after),
            })
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            workspace_after = _workspace_snapshot(self.cwd)
            return Artifact(kind="CodeRevision", producer=self.name, inputs=message.input_artifacts, status="failed", payload={
                "provider": "claude-code", "command": command, "cwd": str(self.cwd),
                "error": "timeout", "timeout_seconds": self.timeout_seconds,
                "workspace_before": workspace_before, "workspace_after": workspace_after,
                "workspace_change_detected": _workspace_changed(workspace_before, workspace_after),
            })
        except (OSError, UnicodeError) as exc:
            workspace_after = _workspace_snapshot(self.cwd)
            return Artifact(kind="CodeRevision", producer=self.name, inputs=message.input_artifacts, status="failed", payload={
                "provider": "claude-code", "command": command, "cwd": str(self.cwd), "error": str(exc),
                "workspace_before": workspace_before, "workspace_after": workspace_after,
                "workspace_change_detected": _workspace_changed(workspace_before, workspace_after),
            })


class CodexCodingAgent:
    """Adapter for the non-interactive OpenAI Codex CLI.

    Authentication is intentionally inherited from the Codex process
    environment (for example ``CODEX_API_KEY``); credentials never enter the
    prompt, argv, or persisted Artifact payload.
    """

    name = "coding"
    capabilities = ("inspect_repo", "implement_experiment", "repair_experiment", "run_tests", "explain_failure")

    def __init__(
        self,
        cwd: str | os.PathLike[str],
        executable: str = "codex",
        timeout_seconds: int = 900,
        extra_args: Sequence[str] = (),
    ) -> None:
        if not executable or any(char in executable for char in "\r\n"):
            raise ValueError("codex executable must be a non-empty single line")
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"coding cwd does not exist: {self.cwd}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if any(not isinstance(arg, str) or not arg for arg in extra_args):
            raise ValueError("extra_args must contain non-empty strings")
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.extra_args = tuple(extra_args)

    def _prompt(self, message: A2AMessage, task: ResearchTask) -> str:
        context = json.dumps(message.input_artifact_data, ensure_ascii=True, separators=(",", ":"))
        if len(context) > 12000:
            context = context[:12000] + "...<truncated>"
        return "\n".join([
            "You are the coding agent in an AutoResearch workflow.",
            f"Research question: {task.question}",
            f"Requested action: {message.action}",
            f"Input Artifact IDs: {', '.join(message.input_artifacts) or '(none)'}",
            f"Input Artifact context (bounded JSON): {context}",
            "Inspect the repository and perform the requested action. For repair_experiment, use the failed ExperimentRun traceback, command, cwd, and environment to diagnose and fix the experiment entry point, arguments, or dependencies, then run a focused verification.",
            "Do not merely explain a failure: when repair_experiment is requested, modify the workspace so Compute can retry it.",
            "Keep changes reproducible and report what changed. Do not fabricate metrics.",
        ])

    def _command(self, prompt: str) -> list[str]:
        return [
            self.executable, "exec", "--json", "--model", "gpt-5.6-terra", "--sandbox", "workspace-write",
            "--skip-git-repo-check", "-C", str(self.cwd), *self.extra_args, prompt,
        ]

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        workspace_before = _workspace_snapshot(self.cwd)
        prompt = self._prompt(message, task)
        command = self._command(prompt)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=str(self.cwd), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            workspace_after = _workspace_snapshot(self.cwd)
            return Artifact(kind="CodeRevision", producer=self.name, inputs=message.input_artifacts,
                status="created" if process.returncode == 0 else "failed", payload={
                    "provider": "codex-cli", "command": command, "cwd": str(self.cwd),
                    "returncode": process.returncode, "stdout": stdout_text, "stderr": stderr_text,
                    "request": {"task_id": message.task_id, "action": message.action, "question": task.question,
                                "input_artifacts": message.input_artifacts, "input_artifact_data": message.input_artifact_data,
                                "parameters": message.parameters},
                    "workspace_before": workspace_before, "workspace_after": workspace_after,
                    "workspace_change_detected": _workspace_changed(workspace_before, workspace_after),
                })
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            workspace_after = _workspace_snapshot(self.cwd)
            return Artifact(kind="CodeRevision", producer=self.name, inputs=message.input_artifacts, status="failed", payload={
                "provider": "codex-cli", "command": command, "cwd": str(self.cwd), "error": "timeout",
                "timeout_seconds": self.timeout_seconds, "workspace_before": workspace_before,
                "workspace_after": workspace_after, "workspace_change_detected": _workspace_changed(workspace_before, workspace_after),
            })
        except (OSError, UnicodeError) as exc:
            workspace_after = _workspace_snapshot(self.cwd)
            return Artifact(kind="CodeRevision", producer=self.name, inputs=message.input_artifacts, status="failed", payload={
                "provider": "codex-cli", "command": command, "cwd": str(self.cwd), "error": str(exc),
                "workspace_before": workspace_before, "workspace_after": workspace_after,
                "workspace_change_detected": _workspace_changed(workspace_before, workspace_after),
            })
