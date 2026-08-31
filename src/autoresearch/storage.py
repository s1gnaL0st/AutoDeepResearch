from __future__ import annotations

import json
import os
import tempfile
import time
import re
from datetime import datetime, timezone
from typing import Any
from dataclasses import asdict
from pathlib import Path

from .models import Artifact, ResearchState, ResearchTask


class ArtifactStore:
    """Simple append-only JSON store; replaceable by PostgreSQL/object storage later."""

    def __init__(self, root: str | Path = ".autoresearch") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(exist_ok=True)
        (self.root / "tasks").mkdir(exist_ok=True)
        (self.root / "jobs").mkdir(exist_ok=True)
        (self.root / "locks").mkdir(exist_ok=True)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Replace a JSON record atomically so readers never see a partial file."""
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # Windows can briefly deny an atomic rename while another thread
            # has just completed a read/write on the same task file (common
            # when the UI polls status while a worker finishes). Retry the
            # rename briefly; readers still never observe a partial record.
            last_error = None
            for attempt in range(8):
                try:
                    os.replace(temp_name, path)
                    last_error = None
                    break
                except PermissionError as exc:
                    last_error = exc
                    if attempt == 7:
                        raise
                    time.sleep(0.025 * (attempt + 1))
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def put_artifact(self, artifact: Artifact) -> Artifact:
        path = self.root / "artifacts" / f"{artifact.artifact_id}.json"
        record = artifact.to_dict()
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != record:
                raise FileExistsError(
                    f"artifact_id {artifact.artifact_id} already exists with different content"
                )
            return artifact
        self._atomic_write(path, json.dumps(record, indent=2, ensure_ascii=True))
        return artifact

    def get_artifact(self, artifact_id: str) -> dict:
        return json.loads((self.root / "artifacts" / f"{artifact_id}.json").read_text(encoding="utf-8"))

    def put_task(self, task: ResearchTask) -> ResearchTask:
        if task.mission_dir is None:
            slug = re.sub(r"[^A-Za-z0-9_-]+", "-", task.question).strip("-")[:48] or "mission"
            mission_path = (self.root / "missions" / f"{slug}-{task.task_id[:8]}" ).resolve()
            mission_path.mkdir(parents=True, exist_ok=True)
            task.mission_dir = str(mission_path)
        path = self.root / "tasks" / f"{task.task_id}.json"
        self._atomic_write(path, json.dumps(asdict(task), indent=2, ensure_ascii=True))
        return task

    def get_task(self, task_id: str) -> ResearchTask:
        path = self.root / "tasks" / f"{task_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        # Backfill legacy tasks created before the durable creation-time field.
        created_at = data.get("created_at") or datetime.fromtimestamp(path.stat().st_ctime, timezone.utc).isoformat()
        return ResearchTask(
            question=data["question"], task_id=data["task_id"], created_at=created_at, state=ResearchState(data["state"]),
            artifacts=list(data.get("artifacts", [])), history=list(data.get("history", [])), error=data.get("error"),
            execution_profile_hash=data.get("execution_profile_hash"),
            execution_status=data.get("execution_status", "not_queued"), job_id=data.get("job_id"),
            attempt=int(data.get("attempt", 0)), max_attempts=int(data.get("max_attempts", 1)),
            cancel_requested=bool(data.get("cancel_requested", False)),
            pause_requested=bool(data.get("pause_requested", False)),
            paused_from_state=ResearchState(data["paused_from_state"]) if data.get("paused_from_state") else None,
            iteration=int(data.get("iteration", 0)), max_iterations=int(data.get("max_iterations", 1)),
            replicates=int(data.get("replicates", 1)),
            objective_metric=data.get("objective_metric", "score"), objective_direction=data.get("objective_direction", "max"),
            best_iteration=data.get("best_iteration"), best_value=data.get("best_value"),
            baseline_requested=bool(data.get("baseline_requested", False)), baseline_artifact_id=data.get("baseline_artifact_id"),
            mission_dir=data.get("mission_dir"), runtime=dict(data.get("runtime", {})),
        )

    def list_tasks(self) -> list[ResearchTask]:
        """Read every persisted task without exposing arbitrary store files."""
        tasks = []
        for path in (self.root / "tasks").glob("*.json"):
            try:
                tasks.append(self.get_task(path.stem))
            except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
                # A malformed task record must not prevent the control plane
                # from showing other recoverable task summaries.
                continue
        return tasks

    def delete_task(self, task_id: str, delete_files: bool = False) -> None:
        """Delete a task and its owned artifacts/jobs (explicit user action)."""
        task = self.get_task(task_id)
        if delete_files and task.mission_dir:
            mission_path = Path(task.mission_dir).resolve()
            missions_root = (self.root / "missions").resolve()
            if missions_root in mission_path.parents:
                import shutil
                shutil.rmtree(mission_path, ignore_errors=False)
        for artifact_id in task.artifacts:
            try: (self.root / "artifacts" / f"{artifact_id}.json").unlink()
            except FileNotFoundError: pass
        for path in (self.root / "jobs").glob("*.json"):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("task_id") == task_id: path.unlink()
            except (FileNotFoundError, json.JSONDecodeError): pass
        try: (self.root / "tasks" / f"{task_id}.json").unlink()
        except FileNotFoundError: pass

    def put_job(self, job_id: str, data: dict[str, Any]) -> dict[str, Any]:
        path = self.root / "jobs" / f"{job_id}.json"
        self._atomic_write(path, json.dumps(data, indent=2, ensure_ascii=True))
        return data

    def get_job(self, job_id: str) -> dict:
        return json.loads((self.root / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))

    def get_job_for_task(self, task_id: str) -> dict | None:
        for path in (self.root / "jobs").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("task_id") == task_id:
                return data
        return None

    def list_jobs(self) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8")) for path in (self.root / "jobs").glob("*.json")]


class PostgresArtifactStore:
    """PostgreSQL-backed store selected with AUTORESEARCH_DATABASE_URL."""

    def __init__(self, dsn: str, root: str | Path = ".autoresearch") -> None:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgreSQL backend requires psycopg") from exc
        self.psycopg = psycopg
        self.dsn = dsn
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS autoresearch_tasks (task_id TEXT PRIMARY KEY, data JSONB NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS autoresearch_artifacts (artifact_id TEXT PRIMARY KEY, data JSONB NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS autoresearch_jobs (job_id TEXT PRIMARY KEY, task_id TEXT, data JSONB NOT NULL)")

    def _connect(self):
        return self.psycopg.connect(self.dsn)

    def put_artifact(self, artifact: Artifact) -> Artifact:
        record = artifact.to_dict()
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM autoresearch_artifacts WHERE artifact_id=%s", (artifact.artifact_id,)).fetchone()
            if row and row[0] != record:
                raise FileExistsError(f"artifact_id {artifact.artifact_id} already exists with different content")
            if not row:
                conn.execute("INSERT INTO autoresearch_artifacts (artifact_id,data) VALUES (%s,%s)", (artifact.artifact_id, self.psycopg.types.json.Json(record)))
        return artifact

    def get_artifact(self, artifact_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM autoresearch_artifacts WHERE artifact_id=%s", (artifact_id,)).fetchone()
        if not row:
            raise FileNotFoundError(artifact_id)
        return row[0]

    def put_task(self, task: ResearchTask) -> ResearchTask:
        if task.mission_dir is None:
            slug = re.sub(r"[^A-Za-z0-9_-]+", "-", task.question).strip("-")[:48] or "mission"
            path = (self.root / "missions" / f"{slug}-{task.task_id[:8]}" ).resolve()
            path.mkdir(parents=True, exist_ok=True)
            task.mission_dir = str(path)
        data = asdict(task)
        with self._connect() as conn:
            conn.execute("INSERT INTO autoresearch_tasks (task_id,data) VALUES (%s,%s) ON CONFLICT (task_id) DO UPDATE SET data=EXCLUDED.data", (task.task_id, self.psycopg.types.json.Json(data)))
        return task

    @staticmethod
    def _task(data: dict) -> ResearchTask:
        return ResearchTask(
            question=data["question"], task_id=data["task_id"], created_at=data.get("created_at"), state=ResearchState(data["state"]),
            artifacts=list(data.get("artifacts", [])), history=list(data.get("history", [])), error=data.get("error"),
            execution_profile_hash=data.get("execution_profile_hash"), execution_status=data.get("execution_status", "not_queued"), job_id=data.get("job_id"),
            attempt=int(data.get("attempt", 0)), max_attempts=int(data.get("max_attempts", 1)), cancel_requested=bool(data.get("cancel_requested", False)), pause_requested=bool(data.get("pause_requested", False)),
            paused_from_state=ResearchState(data["paused_from_state"]) if data.get("paused_from_state") else None, iteration=int(data.get("iteration", 0)), max_iterations=int(data.get("max_iterations", 1)), replicates=int(data.get("replicates", 1)),
            objective_metric=data.get("objective_metric", "score"), objective_direction=data.get("objective_direction", "max"), best_iteration=data.get("best_iteration"), best_value=data.get("best_value"),
            baseline_requested=bool(data.get("baseline_requested", False)), baseline_artifact_id=data.get("baseline_artifact_id"), mission_dir=data.get("mission_dir"), runtime=dict(data.get("runtime", {})),
        )

    def get_task(self, task_id: str) -> ResearchTask:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM autoresearch_tasks WHERE task_id=%s", (task_id,)).fetchone()
        if not row:
            raise FileNotFoundError(task_id)
        return self._task(row[0])

    def list_tasks(self) -> list[ResearchTask]:
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM autoresearch_tasks ORDER BY (data->>'created_at') DESC").fetchall()
        return [self._task(row[0]) for row in rows]

    def delete_task(self, task_id: str, delete_files: bool = False) -> None:
        task = self.get_task(task_id)
        if delete_files and task.mission_dir:
            path = Path(task.mission_dir).resolve()
            if (self.root / "missions").resolve() in path.parents:
                import shutil; shutil.rmtree(path, ignore_errors=True)
        with self._connect() as conn:
            conn.execute("DELETE FROM autoresearch_artifacts WHERE artifact_id = ANY(%s)", (task.artifacts,))
            conn.execute("DELETE FROM autoresearch_jobs WHERE task_id=%s", (task_id,))
            conn.execute("DELETE FROM autoresearch_tasks WHERE task_id=%s", (task_id,))

    def put_job(self, job_id: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("INSERT INTO autoresearch_jobs (job_id,task_id,data) VALUES (%s,%s,%s) ON CONFLICT (job_id) DO UPDATE SET data=EXCLUDED.data, task_id=EXCLUDED.task_id", (job_id, data.get("task_id"), self.psycopg.types.json.Json(data)))
        return data

    def get_job(self, job_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM autoresearch_jobs WHERE job_id=%s", (job_id,)).fetchone()
        if not row: raise FileNotFoundError(job_id)
        return row[0]

    def get_job_for_task(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM autoresearch_jobs WHERE task_id=%s ORDER BY job_id DESC LIMIT 1", (task_id,)).fetchone()
        return row[0] if row else None

    def list_jobs(self) -> list[dict]:
        with self._connect() as conn:
            return [row[0] for row in conn.execute("SELECT data FROM autoresearch_jobs").fetchall()]


def create_store(root: str | Path = ".autoresearch"):
    dsn = os.environ.get("AUTORESEARCH_DATABASE_URL")
    return PostgresArtifactStore(dsn, root) if dsn else ArtifactStore(root)
