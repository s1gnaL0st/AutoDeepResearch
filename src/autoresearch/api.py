from __future__ import annotations

import asyncio
import argparse
import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .cli import resume_research, run_research, build_execution_profile, execution_profile_hash
from .adjudication import build_evidence_adjudication
from .agents import ReportWriterAgent
from .models import ResearchState, ResearchTask
from .protocol import A2AMessage
from .reproducibility import build_reproducibility_package
from .queue import BackgroundTaskRunner
from .storage import create_store
from .status import research_status


_PROFILE_KEYS = {
    "literature", "literature_a2a_url", "deepresearch_command", "deepresearch_cwd", "deerflow_command", "deerflow_cwd", "coding_command", "coding_cwd",
    "compute_command", "compute_cwd", "compute_image", "baseline_command", "baseline_cwd", "auto_approve", "approve",
    "coding_agent", "claude_executable", "codex_executable", "fulltext_paths", "hypothesis_command", "hypothesis_cwd",
    "analysis_command", "analysis_cwd", "reviewer_command", "reviewer_cwd",
    "async", "max_attempts", "a2a_urls", "iterations", "replicates", "objective_metric", "objective_direction", "approve_dependencies",
}


def _task_json(task: ResearchTask) -> dict[str, Any]:
    data = asdict(task)
    data["state"] = task.state.value
    return data


def _task_summary(task: ResearchTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "question": task.question,
        "created_at": task.created_at,
        "state": task.state.value,
        "execution_status": task.execution_status,
        "artifact_count": len(task.artifacts),
        "iteration": task.iteration,
        "max_iterations": task.max_iterations,
        "error": task.error,
    }


def _profile_args(body: dict[str, Any]) -> dict[str, Any]:
    # Action-only fields are validated by their individual handlers below.
    # `delete_files` accompanies DELETE and must not be treated as an
    # execution-profile option.
    unknown = sorted(set(body) - _PROFILE_KEYS - {"question", "delete_files"})
    if unknown:
        raise ValueError(f"unknown request fields: {', '.join(unknown)}")
    literature = body.get("literature", "fixture")
    if literature not in {"fixture", "live", "deepresearch", "deerflow"}:
        raise ValueError("literature must be 'fixture', 'live', 'deepresearch' or 'deerflow'")
    for key in ("deepresearch_command", "coding_command", "compute_command", "baseline_command", "hypothesis_command", "analysis_command", "reviewer_command"):
        value = body.get(key)
        if value is not None and (not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value)):
            raise ValueError(f"{key} must be a non-empty string array")
    if "fulltext_paths" in body and (not isinstance(body["fulltext_paths"], list) or not all(isinstance(item, str) and item.strip() for item in body["fulltext_paths"])):
        raise ValueError("fulltext_paths must be a string array")
    for key in ("deepresearch_cwd", "deerflow_cwd", "coding_cwd", "compute_cwd", "compute_image", "literature_a2a_url", "claude_executable", "codex_executable", "hypothesis_cwd", "analysis_cwd", "reviewer_cwd"):
        value = body.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{key} must be a non-empty string")
    if body.get("compute_image") and not body.get("compute_command"):
        raise ValueError("compute_image requires compute_command")
    if body.get("baseline_command") and not body.get("compute_command"):
        raise ValueError("baseline_command requires compute_command")
    if literature == "deepresearch" and (body.get("literature_a2a_url") or not body.get("deepresearch_command")):
        raise ValueError("deepresearch literature requires deepresearch_command and no literature_a2a_url")
    if literature == "deerflow" and (body.get("literature_a2a_url") or body.get("deepresearch_command")):
        raise ValueError("deerflow literature cannot be combined with another literature adapter")
    coding_agent = body.get("coding_agent", "fake")
    if coding_agent == "fake" and body.get("coding_command"):
        coding_agent = "command"
    if coding_agent not in {"fake", "command", "claude", "codex"}:
        raise ValueError("coding_agent must be 'fake', 'command', 'claude' or 'codex'")
    if coding_agent in {"claude", "codex"} and body.get("coding_command"):
        raise ValueError("coding_command cannot be combined with a CLI coding agent")
    if "auto_approve" in body and not isinstance(body["auto_approve"], bool):
        raise ValueError("auto_approve must be boolean")
    iterations = body.get("iterations")
    if iterations is not None and (not isinstance(iterations, int) or isinstance(iterations, bool) or not 1 <= iterations <= 20):
        raise ValueError("iterations must be between 1 and 20")
    replicates = body.get("replicates")
    if replicates is not None and (not isinstance(replicates, int) or isinstance(replicates, bool) or not 1 <= replicates <= 20):
        raise ValueError("replicates must be between 1 and 20")
    objective_metric = body.get("objective_metric", "score")
    objective_direction = body.get("objective_direction", "max")
    if not isinstance(objective_metric, str) or not objective_metric.strip():
        raise ValueError("objective_metric must be a non-empty string")
    if objective_direction not in {"max", "min"}:
        raise ValueError("objective_direction must be 'max' or 'min'")
    a2a_urls = body.get("a2a_urls", {})
    if not isinstance(a2a_urls, dict) or not all(isinstance(stage, str) and isinstance(url, str) and stage and url for stage, url in a2a_urls.items()):
        raise ValueError("a2a_urls must be an object mapping stage names to URLs")
    return {
        "literature_mode": literature,
        "literature_a2a_url": body.get("literature_a2a_url"),
        "deepresearch_command": body.get("deepresearch_command"),
        "deepresearch_cwd": body.get("deepresearch_cwd"),
        "deerflow_command": body.get("deerflow_command"),
        "deerflow_cwd": body.get("deerflow_cwd"),
        "coding_agent": coding_agent,
        "claude_executable": body.get("claude_executable", "claude"),
        "codex_executable": body.get("codex_executable", "codex"),
        "coding_command": body.get("coding_command"),
        "coding_cwd": body.get("coding_cwd"),
        "compute_command": body.get("compute_command"),
        "compute_cwd": body.get("compute_cwd"),
        "compute_image": body.get("compute_image"),
        "baseline_command": body.get("baseline_command"),
        "baseline_cwd": body.get("baseline_cwd"),
        "a2a_urls": dict(a2a_urls),
        "iterations": iterations,
        "replicates": replicates,
        "fulltext_paths": body.get("fulltext_paths", []),
        "hypothesis_command": body.get("hypothesis_command"),
        "hypothesis_cwd": body.get("hypothesis_cwd"),
        "analysis_command": body.get("analysis_command"),
        "analysis_cwd": body.get("analysis_cwd"),
        "reviewer_command": body.get("reviewer_command"),
        "reviewer_cwd": body.get("reviewer_cwd"),
        "objective_metric": objective_metric,
        "objective_direction": objective_direction,
    }


class ResearchApiServer:
    """Small local control-plane API for creating and resuming research tasks.

    The handler intentionally runs one workflow per request. It is suitable for
    local integration and contract tests; a queue/worker service should replace
    this execution model for production workloads.
    """

    def __init__(self, root: str = ".autoresearch", host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("development server only permits a loopback host")
        self.root = root
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._runner = BackgroundTaskRunner(create_store(root))
        self._runner.recover_orphaned()

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("server has not been started")
        return f"http://{self.host}:{self._server.server_address[1]}"

    def start(self) -> "ResearchApiServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                content = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                # The bundled UI is intentionally a separate static origin
                # (for example `python -m http.server 5173 -d frontend`).
                # Keep this development API browser-friendly; the loopback
                # host restriction still prevents accidental public exposure.
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                if length == 0:
                    return {}
                value = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("request body must be a JSON object")
                return value

            def _task(self, task_id: str) -> ResearchTask:
                return create_store(outer.root).get_task(task_id)

            def _task_artifacts(self, task: ResearchTask) -> list[dict[str, Any]]:
                """Read Artifacts in the task's immutable provenance order."""
                store = create_store(outer.root)
                return [store.get_artifact(artifact_id) for artifact_id in task.artifacts]

            def do_GET(self) -> None:
                request_url = urlparse(self.path)
                path = request_url.path.rstrip("/")
                if path == "/research":
                    query = parse_qs(request_url.query, keep_blank_values=True)
                    unknown = sorted(set(query) - {"state", "execution_status"})
                    if unknown:
                        self._send(400, {"error": "invalid_request", "message": f"unknown query fields: {', '.join(unknown)}"})
                        return
                    states = query.get("state", [])
                    if any(value not in {state.value for state in ResearchState} for value in states):
                        self._send(400, {"error": "invalid_request", "message": "state must be a valid ResearchState value"})
                        return
                    execution_statuses = query.get("execution_status", [])
                    if any(not value for value in execution_statuses):
                        self._send(400, {"error": "invalid_request", "message": "execution_status must be non-empty"})
                        return
                    tasks = create_store(outer.root).list_tasks()
                    if states:
                        tasks = [task for task in tasks if task.state.value in states]
                    if execution_statuses:
                        tasks = [task for task in tasks if task.execution_status in execution_statuses]
                    tasks.sort(key=lambda task: (task.history[-1].get("at", "") if task.history else "", task.task_id), reverse=True)
                    self._send(200, {"tasks": [_task_summary(task) for task in tasks]})
                    return
                if path.startswith("/research/") and path.endswith("/job"):
                    task_id = path.split("/")[2]
                    job = outer._runner.get_job_for_task(task_id)
                    if job is None:
                        self._send(404, {"error": "job_not_found", "task_id": task_id})
                    else:
                        self._send(200, {"job": job})
                    return
                if path.startswith("/research/") and path.endswith("/status"):
                    parts = path.split("/")
                    if len(parts) != 4 or parts[1] != "research" or parts[3] != "status":
                        self._send(404, {"error": "not_found"})
                        return
                    task_id = parts[2]
                    try:
                        task = self._task(task_id)
                        self._send(200, {"status": research_status(task, create_store(outer.root))})
                    except FileNotFoundError:
                        self._send(404, {"error": "task_not_found", "task_id": task_id})
                    return
                if path.startswith("/research/") and "/artifacts/" in path:
                    parts = path.split("/")
                    if len(parts) != 5 or parts[1] != "research" or parts[3] != "artifacts":
                        self._send(404, {"error": "not_found"})
                        return
                    task_id, artifact_id = parts[2], parts[4]
                    try:
                        task = self._task(task_id)
                    except FileNotFoundError:
                        self._send(404, {"error": "task_not_found", "task_id": task_id})
                        return
                    if artifact_id not in task.artifacts:
                        self._send(404, {"error": "artifact_not_found", "task_id": task_id, "artifact_id": artifact_id})
                        return
                    try:
                        artifact = create_store(outer.root).get_artifact(artifact_id)
                    except FileNotFoundError:
                        self._send(404, {"error": "artifact_not_found", "task_id": task_id, "artifact_id": artifact_id})
                        return
                    self._send(200, {"task_id": task_id, "artifact": artifact})
                    return
                if path.startswith("/research/") and path.endswith("/artifacts"):
                    parts = path.split("/")
                    if len(parts) != 4 or parts[1] != "research" or parts[3] != "artifacts":
                        self._send(404, {"error": "not_found"})
                        return
                    task_id = parts[2]
                    try:
                        task = self._task(task_id)
                    except FileNotFoundError:
                        self._send(404, {"error": "task_not_found", "task_id": task_id})
                        return
                    try:
                        artifacts = self._task_artifacts(task)
                    except FileNotFoundError:
                        self._send(404, {"error": "artifact_not_found", "task_id": task_id})
                        return
                    self._send(200, {"task_id": task_id, "artifacts": artifacts})
                    return
                if path.startswith("/research/") and path.count("/") == 2:
                    task_id = path.rsplit("/", 1)[1]
                    try:
                        self._send(200, {"task": _task_json(self._task(task_id))})
                    except FileNotFoundError:
                        self._send(404, {"error": "task_not_found", "task_id": task_id})
                    return
                self._send(404, {"error": "not_found"})

            def do_POST(self) -> None:
                path = urlparse(self.path).path.rstrip("/")
                try:
                    body = self._body()
                    if path == "/research":
                        question = body.get("question")
                        if not isinstance(question, str) or not question.strip():
                            raise ValueError("question must be a non-empty string")
                        profile = _profile_args(body)
                        profile_values = (
                            profile["literature_mode"], profile["coding_command"], profile["coding_cwd"],
                            profile["compute_command"], profile["compute_cwd"], profile["compute_image"],
                            profile["literature_a2a_url"],
                        )
                        if bool(body.get("async", False)):
                            # Create and queue immediately; literature search now
                            # runs in the worker instead of blocking initialization.
                            task = ResearchTask(question, execution_profile_hash=execution_profile_hash(build_execution_profile(
                                profile["literature_mode"], profile["deepresearch_command"], profile["deepresearch_cwd"], profile["deerflow_command"], profile["deerflow_cwd"], profile["coding_agent"], profile["claude_executable"], profile["codex_executable"], profile["coding_command"], profile["coding_cwd"], profile["compute_command"], profile["compute_cwd"], profile["compute_image"], profile["literature_a2a_url"], profile["a2a_urls"], profile["iterations"] or 1, profile["objective_metric"], profile["objective_direction"], profile["baseline_command"], profile["baseline_cwd"], profile["replicates"] or 1, profile["fulltext_paths"], profile["hypothesis_command"], profile["hypothesis_cwd"], profile["analysis_command"], profile["analysis_cwd"], profile["reviewer_command"], profile["reviewer_cwd"]
                            )), max_iterations=profile["iterations"] or 1, replicates=profile["replicates"] or 1, objective_metric=profile["objective_metric"], objective_direction=profile["objective_direction"], baseline_requested=bool(profile["baseline_command"]))
                            outer._runner.store.put_task(task)
                            job = outer._runner.submit(task.task_id, outer._resume_callback(task.task_id, profile), int(body.get("max_attempts", 1)))
                            self._send(202, {"task": _task_json(outer._runner.store.get_task(task.task_id)), "job": job.to_dict()})
                        else:
                            task = asyncio.run(run_research(
                                question, outer.root, *profile_values,
                                auto_approve=bool(body.get("auto_approve", False)),
                                coding_agent=profile["coding_agent"], claude_executable=profile["claude_executable"], codex_executable=profile["codex_executable"],
                                deepresearch_command=profile["deepresearch_command"], deepresearch_cwd=profile["deepresearch_cwd"],
                                a2a_urls=profile["a2a_urls"],
                                deerflow_command=profile["deerflow_command"], deerflow_cwd=profile["deerflow_cwd"],
                                iterations=profile["iterations"] or 1,
                                replicates=profile["replicates"] or 1,
                                fulltext_paths=profile["fulltext_paths"],
                                hypothesis_command=profile["hypothesis_command"], hypothesis_cwd=profile["hypothesis_cwd"],
                                analysis_command=profile["analysis_command"], analysis_cwd=profile["analysis_cwd"],
                                reviewer_command=profile["reviewer_command"], reviewer_cwd=profile["reviewer_cwd"],
                                objective_metric=profile["objective_metric"], objective_direction=profile["objective_direction"],
                                baseline_command=profile["baseline_command"], baseline_cwd=profile["baseline_cwd"],
                            ))
                            self._send(201, {"task": _task_json(task)})
                        return

                    if path.startswith("/research/"):
                        parts = path.split("/")
                        if len(parts) != 4 or parts[3] not in {"approve", "resume", "retry", "cancel", "pause", "delete", "adjudicate"}:
                            self._send(404, {"error": "not_found"})
                            return
                        task_id, action = parts[2], parts[3]
                        if action == "adjudicate":
                            task = outer._runner.store.get_task(task_id)
                            if task.state.value != "REPORT_READY":
                                raise ValueError("evidence adjudication requires a REPORT_READY task")
                            adjudicator = body.get("adjudicator")
                            decisions = body.get("decisions")
                            records = [outer._runner.store.get_artifact(artifact_id) for artifact_id in task.artifacts]
                            claim_map = next((record for record in reversed(records) if record.get("kind") == "ClaimEvidenceMap"), None)
                            if claim_map is None:
                                raise ValueError("task has no ClaimEvidenceMap")
                            adjudication = build_evidence_adjudication(claim_map, decisions, adjudicator=adjudicator)
                            outer._runner.store.put_artifact(adjudication)
                            task.artifacts.append(adjudication.artifact_id)
                            package = build_reproducibility_package(task, outer._runner.store)
                            outer._runner.store.put_artifact(package)
                            task.artifacts.append(package.artifact_id)
                            report_inputs = [outer._runner.store.get_artifact(artifact_id) for artifact_id in task.artifacts]
                            report = asyncio.run(ReportWriterAgent().handle(
                                A2AMessage(task.task_id, "control-plane", "report", "write_report", list(task.artifacts), report_inputs), task,
                            ))
                            if report.status != "created":
                                raise RuntimeError("report regeneration failed after adjudication")
                            outer._runner.store.put_artifact(report)
                            task.artifacts.append(report.artifact_id)
                            outer._runner.store.put_task(task)
                            self._send(200, {"task": _task_json(task), "adjudication": adjudication.to_dict(), "report": report.to_dict()})
                            return
                        profile = _profile_args(body)
                        if action == "cancel":
                            job = outer._runner.cancel(task_id)
                            self._send(200, {"job": job})
                            return
                        if action == "pause":
                            job = outer._runner.pause(task_id)
                            self._send(202, {"task": _task_json(outer._runner.store.get_task(task_id)), "job": job})
                            return
                        if action == "delete":
                            try:
                                task = outer._runner.store.get_task(task_id)
                            except FileNotFoundError:
                                # DELETE is idempotent: a stale UI row may
                                # refer to a task already removed elsewhere.
                                self._send(200, {"deleted": task_id, "already_absent": True, "files_deleted": False})
                                return
                            active = next((j for j in outer._runner.store.list_jobs() if j.get("task_id") == task_id and j.get("status") in {"queued", "running"}), None)
                            if active:
                                raise ValueError("cannot delete a running task; pause or cancel it first")
                            delete_files = bool(body.get("delete_files", False))
                            outer._runner.store.delete_task(task_id, delete_files=delete_files)
                            self._send(200, {"deleted": task_id, "files_deleted": delete_files})
                            return
                        current_task = outer._runner.store.get_task(task_id)
                        if action == "resume" and (current_task.state == ResearchState.AWAITING_DEPENDENCY_APPROVAL or current_task.runtime.get("phase") == "awaiting_dependency_approval"):
                            # Do not let omitted request fields inherit parser
                            # defaults and override the mission objective.
                            for field in ("iterations", "replicates", "objective_metric", "objective_direction"):
                                if field not in body:
                                    profile[field] = None
                            approved = body.get("approve_dependencies")
                            if not isinstance(approved, bool):
                                raise ValueError("approve_dependencies must be boolean for a dependency gate")
                            request = dict(current_task.runtime.get("dependency_request", {}))
                            current_task.runtime = {
                                **current_task.runtime,
                                "dependency_approval": approved,
                                "dependency_approval_at": datetime.now(timezone.utc).isoformat(),
                            }
                            if not approved:
                                current_task.execution_status = "paused"
                                current_task.error = "用户拒绝安装实验依赖；任务已暂停，可 Resume 重新询问"
                                current_task.runtime["dependency_request"] = request
                                outer._runner.store.put_task(current_task)
                                self._send(200, {"task": _task_json(current_task), "dependency_request": request, "paused": True})
                                return
                            current_task.error = None
                            # Clear the visible dependency gate before the
                            # background worker starts pip. Otherwise a UI
                            # polling between approval and worker startup can
                            # keep rendering the old "review dependencies"
                            # action instead of the normal pause control.
                            current_task.runtime["phase"] = "dependency_install_approved"
                            if current_task.state == ResearchState.AWAITING_DEPENDENCY_APPROVAL:
                                current_task.transition(ResearchState.IMPLEMENTING)
                            current_task.execution_status = "not_queued"
                            outer._runner.store.put_task(current_task)
                            job = outer._runner.submit(task_id, outer._resume_callback(task_id, profile), int(body.get("max_attempts", 1)))
                            self._send(202, {"task": _task_json(outer._runner.store.get_task(task_id)), "job": job.to_dict()})
                            return
                        if action == "resume" and current_task.state == ResearchState.PAUSED:
                            job = outer._runner.resume(task_id, outer._resume_callback(task_id, profile), int(body.get("max_attempts", 1)))
                            self._send(202, {"task": _task_json(outer._runner.store.get_task(task_id)), "job": job.to_dict()})
                            return
                        if action == "retry":
                            task = outer._runner.store.get_task(task_id)
                            if task.execution_status == "orphaned":
                                task.prepare_orphaned_retry()
                            elif task.state == ResearchState.FAILED or task.execution_status == "failed":
                                if task.state != ResearchState.FAILED:
                                    task.transition(ResearchState.FAILED)
                                has_code = any(outer._runner.store.get_artifact(aid).get("kind") == "CodeRevision" for aid in task.artifacts)
                                task.reset_for_retry(ResearchState.IMPLEMENTING if has_code else ResearchState.DRAFT)
                            else:
                                raise ValueError("retry requires an orphaned or failed task")
                            outer._runner.store.put_task(task)
                            profile = _profile_args(body)
                            # Omitted execution-shape fields mean "reuse the
                            # persisted task profile". Passing None lets
                            # resume_research derive those values from task
                            # state instead of changing the profile hash.
                            if "iterations" not in body:
                                profile["iterations"] = None
                            if "replicates" not in body:
                                profile["replicates"] = None
                            if "objective_metric" not in body:
                                profile["objective_metric"] = None
                            if "objective_direction" not in body:
                                profile["objective_direction"] = None
                            job = outer._runner.submit(task_id, outer._resume_callback(task_id, profile), int(body.get("max_attempts", 1)))
                            self._send(202, {"task": _task_json(outer._runner.store.get_task(task_id)), "job": job.to_dict()})
                            return
                        if "objective_metric" not in body:
                            profile["objective_metric"] = None
                        if "objective_direction" not in body:
                            profile["objective_direction"] = None
                        if "replicates" not in body:
                            profile["replicates"] = None
                        approve = action == "approve" or bool(body.get("approve", True))
                        if bool(body.get("async", False)):
                            task = outer._runner.store.get_task(task_id)
                            if approve:
                                job = outer._runner.submit(task_id, outer._resume_callback(task_id, profile), int(body.get("max_attempts", 1)))
                                self._send(202, {"task": _task_json(outer._runner.store.get_task(task_id)), "job": job.to_dict()})
                            else:
                                self._send(200, {"task": _task_json(task)})
                        else:
                            task = asyncio.run(resume_research(
                                task_id, outer.root, approve=approve,
                                literature_mode=profile["literature_mode"], coding_command=profile["coding_command"],
                                coding_cwd=profile["coding_cwd"], compute_command=profile["compute_command"],
                                compute_cwd=profile["compute_cwd"], compute_image=profile["compute_image"],
                                literature_a2a_url=profile["literature_a2a_url"], coding_agent=profile["coding_agent"],
                                claude_executable=profile["claude_executable"], codex_executable=profile["codex_executable"],
                                deepresearch_command=profile["deepresearch_command"], deepresearch_cwd=profile["deepresearch_cwd"],
                                a2a_urls=profile["a2a_urls"],
                                deerflow_command=profile["deerflow_command"], deerflow_cwd=profile["deerflow_cwd"],
                                iterations=profile["iterations"],
                                replicates=profile["replicates"],
                                fulltext_paths=profile["fulltext_paths"],
                                hypothesis_command=profile["hypothesis_command"], hypothesis_cwd=profile["hypothesis_cwd"],
                                analysis_command=profile["analysis_command"], analysis_cwd=profile["analysis_cwd"],
                                reviewer_command=profile["reviewer_command"], reviewer_cwd=profile["reviewer_cwd"],
                                objective_metric=profile["objective_metric"], objective_direction=profile["objective_direction"],
                                baseline_command=profile["baseline_command"], baseline_cwd=profile["baseline_cwd"],
                            ))
                            self._send(200, {"task": _task_json(task)})
                        return
                    self._send(404, {"error": "not_found"})
                except FileNotFoundError:
                    self._send(404, {"error": "task_not_found"})
                except (ValueError, json.JSONDecodeError, TypeError) as exc:
                    self._send(400, {"error": "invalid_request", "message": str(exc)})
                except Exception as exc:
                    self._send(409, {"error": "workflow_failed", "message": str(exc)})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def _resume_callback(self, task_id: str, profile: dict[str, Any]):
        def execute() -> ResearchTask:
            return asyncio.run(resume_research(
                task_id, self.root, approve=True,
                literature_mode=profile["literature_mode"], coding_command=profile["coding_command"],
                coding_cwd=profile["coding_cwd"], compute_command=profile["compute_command"],
                compute_cwd=profile["compute_cwd"], compute_image=profile["compute_image"],
                literature_a2a_url=profile["literature_a2a_url"], coding_agent=profile["coding_agent"],
                claude_executable=profile["claude_executable"], codex_executable=profile["codex_executable"],
                deepresearch_command=profile["deepresearch_command"], deepresearch_cwd=profile["deepresearch_cwd"],
                a2a_urls=profile["a2a_urls"],
                deerflow_command=profile["deerflow_command"], deerflow_cwd=profile["deerflow_cwd"],
                iterations=profile["iterations"],
                replicates=profile["replicates"],
                fulltext_paths=profile["fulltext_paths"],
                hypothesis_command=profile["hypothesis_command"], hypothesis_cwd=profile["hypothesis_cwd"],
                analysis_command=profile["analysis_command"], analysis_cwd=profile["analysis_cwd"],
                reviewer_command=profile["reviewer_command"], reviewer_cwd=profile["reviewer_cwd"],
                objective_metric=profile["objective_metric"], objective_direction=profile["objective_direction"],
                baseline_command=profile["baseline_command"], baseline_cwd=profile["baseline_cwd"],
            ))
        return execute

    def close(self) -> None:
        self._runner.close()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> "ResearchApiServer":
        return self.start()

    def __exit__(self, *args: object) -> None:
        self.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="autoresearch-api")
    parser.add_argument("--store", default=".autoresearch")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    server = ResearchApiServer(args.store, args.host, args.port).start()
    print(f"AutoResearch API listening at {server.base_url}")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        server.close()


if __name__ == "__main__":
    main()
