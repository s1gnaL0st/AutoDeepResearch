from __future__ import annotations

import os
import threading
import uuid
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Callable, Iterator

from .models import ResearchState, ResearchTask
from .workflow import WorkflowDependencyApprovalRequired, WorkflowPaused
from .storage import ArtifactStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobRecord:
    job_id: str
    task_id: str
    status: str = "queued"
    attempt: int = 0
    max_attempts: int = 1
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now()

    def to_dict(self) -> dict:
        return asdict(self)


class TaskAlreadyQueued(RuntimeError):
    pass


@contextmanager
def task_lock(store: ArtifactStore, task_id: str) -> Iterator[None]:
    """Acquire a cross-process lock using an atomic lock-file create."""
    path = store.root / "locks" / f"{task_id}.lock"
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise TaskAlreadyQueued(f"task {task_id} is already running") from exc
    try:
        os.write(fd, f"pid={os.getpid()}\ncreated_at={_now()}\n".encode("ascii"))
        os.close(fd)
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _lock_pid(path) -> int | None:
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            if line.startswith("pid="):
                return int(line.split("=", 1)[1])
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def _process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    if os.name == "nt":
        # Windows does not give signal 0 POSIX probe semantics: os.kill(pid, 0)
        # can terminate the target process. Query the process exit code instead.
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid,
        )
        if not handle:
            # An access-denied query is deliberately treated as alive: taking
            # over an uninspectable lock is less safe than leaving it orphaned.
            return ctypes.get_last_error() == error_access_denied
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return ctypes.get_last_error() == error_access_denied
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


class BackgroundTaskRunner:
    """Persistent job metadata plus bounded in-process execution workers.

    Job state survives process inspection and API restarts. Callbacks are kept
    in memory by design because execution profiles can contain sensitive or
    operator-controlled commands; orphaned jobs must be explicitly resubmitted
    with their profile after a process restart.
    """

    def __init__(self, store: ArtifactStore, max_workers: int = 2) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="autoresearch")
        self._futures: dict[str, Future] = {}
        self._mutex = threading.RLock()

    def submit(self, task_id: str, execute: Callable[[], ResearchTask], max_attempts: int = 1) -> JobRecord:
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        with self._mutex:
            task = self.store.get_task(task_id)
            if task.state not in {ResearchState.DRAFT, ResearchState.AWAITING_APPROVAL, ResearchState.IMPLEMENTING, ResearchState.AWAITING_DEPENDENCY_APPROVAL, ResearchState.FAILED}:
                raise ValueError(f"task {task_id} cannot be queued from state {task.state.value}")
            if task.execution_status in {"queued", "running"}:
                raise TaskAlreadyQueued(f"task {task_id} is already queued")
            for job in self.store.list_jobs():
                if job.get("task_id") == task_id and job.get("status") in {"queued", "running"}:
                    raise TaskAlreadyQueued(f"task {task_id} is already queued")
            job = JobRecord(str(uuid.uuid4()), task_id, max_attempts=max_attempts)
            self.store.put_job(job.job_id, job.to_dict())
            task.execution_status = "queued"
            task.job_id = job.job_id
            task.attempt = 0
            task.max_attempts = max_attempts
            task.cancel_requested = False
            self.store.put_task(task)
            self._futures[job.job_id] = self.executor.submit(self._run, job, execute)
            return job

    def _save(self, job: JobRecord, task: ResearchTask | None = None) -> None:
        with self._mutex:
            self.store.put_job(job.job_id, job.to_dict())
            if task is not None:
                self.store.put_task(task)

    def _run(self, job: JobRecord, execute: Callable[[], ResearchTask]) -> None:
        try:
            with task_lock(self.store, job.task_id):
                for attempt in range(1, job.max_attempts + 1):
                    task = self.store.get_task(job.task_id)
                    if task.cancel_requested:
                        job.status = "cancelled"
                        job.finished_at = _now()
                        task.execution_status = "cancelled"
                        self._save(job, task)
                        return
                    job.status = "running"
                    job.attempt = attempt
                    job.started_at = job.started_at or _now()
                    task.execution_status = "running"
                    task.attempt = attempt
                    self._save(job, task)
                    try:
                        result = execute()
                        task = result
                        # The callback may have held a stale in-memory task while
                        # the API set the cancellation flag from another thread.
                        # Re-read the durable flag before declaring success.
                        persisted = self.store.get_task(job.task_id)
                        if persisted.cancel_requested:
                            task.cancel_requested = True
                        if task.cancel_requested:
                            job.status = "cancelled"
                            task.execution_status = "cancelled"
                            job.error = "cancel requested"
                            job.finished_at = _now()
                            self._save(job, task)
                            return
                        if task.state == ResearchState.AWAITING_DEPENDENCY_APPROVAL:
                            job.status = "paused"
                            job.finished_at = _now()
                            job.error = task.error
                            task.execution_status = "awaiting_dependency_approval"
                            self._save(job, task)
                            return
                        if task.state == ResearchState.REPORT_READY:
                            job.status = "succeeded"
                            task.execution_status = "succeeded"
                            job.finished_at = _now()
                            self._save(job, task)
                            return
                        error = task.error or f"workflow ended in {task.state.value}"
                    except WorkflowPaused as exc:
                        job.status = "paused"
                        job.finished_at = _now()
                        task = self.store.get_task(job.task_id)
                        task.execution_status = "paused"
                        task.error = str(exc)
                        self._save(job, task)
                        return
                    except Exception as exc:  # callback failure is recorded and bounded
                        task = self.store.get_task(job.task_id)
                        error = str(exc)
                    job.error = error
                    if attempt < job.max_attempts:
                        if task.state == ResearchState.FAILED:
                            task.reset_for_retry()
                        task.execution_status = "queued"
                        task.error = error
                        self._save(job, task)
                        continue
                    job.status = "failed"
                    job.finished_at = _now()
                    task.execution_status = "failed"
                    task.error = error
                    # Callback/profile failures can precede the workflow's
                    # own exception handler. Do not leave an active stage
                    # visible when the background job is already terminal.
                    if task.state not in {ResearchState.FAILED, ResearchState.CANCELLED, ResearchState.REPORT_READY}:
                        task.transition(ResearchState.FAILED)
                        task.error = error
                    self._save(job, task)
        except TaskAlreadyQueued as exc:
            task = self.store.get_task(job.task_id)
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = _now()
            task.execution_status = "failed"
            task.error = str(exc)
            self._save(job, task)
        finally:
            with self._mutex:
                self._futures.pop(job.job_id, None)

    def cancel(self, task_id: str) -> dict:
        with self._mutex:
            job = next((item for item in self.store.list_jobs() if item.get("task_id") == task_id and item.get("status") in {"queued", "running"}), None)
            task = self.store.get_task(task_id)
            if job is None:
                raise ValueError(f"task {task_id} has no active job")
            task.cancel_requested = True
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["finished_at"] = _now()
                task.execution_status = "cancelled"
                future = self._futures.get(job["job_id"])
                if future is not None:
                    future.cancel()
            else:
                task.execution_status = "cancellation_requested"
            self.store.put_task(task)
            self.store.put_job(job["job_id"], job)
            return job

    def pause(self, task_id: str) -> dict:
        with self._mutex:
            job = next((item for item in self.store.list_jobs() if item.get("task_id") == task_id and item.get("status") in {"queued", "running"}), None)
            if job is None:
                raise ValueError(f"task {task_id} has no active job")
            task = self.store.get_task(task_id)
            task.pause_requested = True
            task.execution_status = "pause_requested"
            self.store.put_task(task)
            return job

    def resume(self, task_id: str, execute: Callable[[], ResearchTask], max_attempts: int = 1) -> JobRecord:
        task = self.store.get_task(task_id)
        if task.state != ResearchState.PAUSED:
            raise ValueError("task is not paused")
        # Resume at the workflow's safe checkpoint. Agent calls are atomic, so
        # a pause request is observed between stages; re-entering approval
        # avoids replaying a partially completed subprocess.
        task.state = ResearchState.AWAITING_APPROVAL
        task.paused_from_state = None
        task.pause_requested = False
        task.error = None
        self.store.put_task(task)
        return self.submit(task_id, execute, max_attempts)

    def recover_orphaned(self) -> list[dict]:
        """Mark jobs from a previous process as orphaned, never execute blindly."""
        recovered = []
        with self._mutex:
            for job in self.store.list_jobs():
                if job.get("status") not in {"queued", "running"}:
                    continue
                lock_path = self.store.root / "locks" / f"{job['task_id']}.lock"
                # A second worker may be alive. Do not mark its job orphaned or
                # remove its lock merely because this process was restarted.
                if lock_path.exists() and _process_alive(_lock_pid(lock_path)):
                    continue
                job["status"] = "orphaned"
                job["finished_at"] = _now()
                job["error"] = "worker process restarted; resubmit with execution profile"
                self.store.put_job(job["job_id"], job)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    task = self.store.get_task(job["task_id"])
                    task.execution_status = "orphaned"
                    task.error = job["error"]
                    # An orphan is no longer executing. Move the task to the
                    # explicit FAILED state so the UI does not show a spinner
                    # or imply that work is still progressing; retry can
                    # later resume from its existing artifacts.
                    if task.state not in {ResearchState.FAILED, ResearchState.CANCELLED, ResearchState.REPORT_READY}:
                        task.transition(ResearchState.FAILED)
                    self.store.put_task(task)
                except FileNotFoundError:
                    pass
                recovered.append(job)
        return recovered

    def get_job_for_task(self, task_id: str) -> dict | None:
        jobs = [job for job in self.store.list_jobs() if job.get("task_id") == task_id]
        return max(jobs, key=lambda job: job.get("created_at", ""), default=None)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> "BackgroundTaskRunner":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
