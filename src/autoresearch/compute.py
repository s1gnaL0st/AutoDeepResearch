from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import Artifact, ResearchTask
from .protocol import A2AMessage
from .storage import ArtifactStore


_REQUIREMENT_IMPORT_ALIASES = {
    "scikit-learn": "sklearn",
    "scikit-image": "skimage",
    "pillow": "PIL",
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "pyyaml": "yaml",
}


def _requirements_packages(path: Path) -> list[tuple[str, str]]:
    """Read importable top-level packages from an explicit requirements file."""
    packages: list[tuple[str, str]] = []
    if not path.is_file():
        return packages
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http:" , "https:")):
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
        if not match:
            continue
        distribution = match.group(1)
        normalized = distribution.lower().replace("_", "-")
        import_name = _REQUIREMENT_IMPORT_ALIASES.get(normalized, distribution.replace("-", "_"))
        packages.append((distribution, import_name))
    return packages


def _numeric_metrics(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    metrics: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, (int, float)) or isinstance(item, bool):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        metrics[key] = number
    return metrics


def _json_candidates(stdout: str) -> list[str]:
    """Return complete JSON candidates from noisy process output."""
    text = stdout or ""
    embedded: list[str] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            _, end = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        embedded.append(text[match.start():match.start() + end])
    # Put broad embedded scans first so the complete stdout/marked result is
    # preferred when iterating in reverse (avoids selecting a nested model
    # object instead of the enclosing {"models": ...} report).
    candidates = embedded + [text.strip()]
    candidates.extend(
        line.split("AUTORESEARCH_RESULT:", 1)[1].strip()
        for line in text.splitlines() if "AUTORESEARCH_RESULT:" in line
    )
    candidates.extend(match.group(1).strip() for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.I | re.S))
    return candidates


def _extract_scalar_metrics(data: Mapping[str, Any]) -> dict[str, float] | None:
    """Extract metrics from canonical, nested, or flat report shapes."""
    for key in ("metrics", "result", "evaluation", "summary"):
        nested = data.get(key)
        metrics = _numeric_metrics(nested)
        if metrics:
            return metrics
    metrics = {
        key: float(value) for key, value in data.items()
        if isinstance(key, str) and isinstance(value, (int, float))
        and not isinstance(value, bool) and math.isfinite(float(value))
    }
    return metrics or None


def extract_result(stdout: str, objective_metric: str | None = None) -> tuple[dict[str, float] | None, str]:
    """Read metrics from JSON stdout or an AUTORESEARCH_RESULT line.

    In addition to the canonical ``{"metrics": {...}}`` contract, accept the
    common model-comparison shape ``{"models": {name: {"accuracy": ...}}}``.
    The best model is selected for the requested objective; ``score`` is
    treated as an alias for accuracy when no explicit score is emitted.
    """
    candidates = _json_candidates(stdout)
    for candidate in reversed(candidates):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        # Canonical output is {"metrics": {...}}.  Also accept a flat result
        # object emitted by many existing experiment scripts, e.g. a report
        # containing score plus descriptive metadata such as experiment name,
        # train/test counts, or random seed.  Non-numeric fields are metadata,
        # not a reason to discard otherwise valid scalar metrics.
        metrics = _extract_scalar_metrics(data) if isinstance(data, Mapping) else None
        if metrics is not None and not (isinstance(data, Mapping) and isinstance(data.get("models"), Mapping)):
            return metrics, "json"
        if isinstance(data, Mapping) and isinstance(data.get("models"), Mapping):
            models = []
            for model in data["models"].values():
                model_metrics = _numeric_metrics(model)
                if model_metrics is None and isinstance(model, Mapping):
                    # Model reports often include nested confusion matrices;
                    # retain only scalar summary metrics for the objective.
                    model_metrics = {k: float(v) for k, v in model.items()
                                     if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
                                     and math.isfinite(float(v))}
                if model_metrics:
                    models.append(model_metrics)
            if models:
                metric = objective_metric or "accuracy"
                key = metric if any(metric in item for item in models) else ("accuracy" if any("accuracy" in item for item in models) else next(iter(models[0])))
                best = max(models, key=lambda item: item.get(key, float("-inf")))
                result = dict(best)
                if objective_metric and objective_metric not in result and objective_metric == "score" and "accuracy" in result:
                    result[objective_metric] = result["accuracy"]
                return result, "json_models"
    return None, "missing_or_invalid"


def _environment() -> dict[str, str]:
    return {"python": platform.python_version(), "platform": platform.platform()}


class LocalComputeAgent:
    """Execute a reproducible experiment command on the host with bounded runtime."""

    name = "compute"
    capabilities = ("run_experiment", "collect_metrics")

    def __init__(self, command: Sequence[str], cwd: str | os.PathLike[str], timeout_seconds: int = 900, env: Mapping[str, str] | None = None, baseline_command: Sequence[str] | None = None, baseline_cwd: str | os.PathLike[str] | None = None) -> None:
        if not command or not command[0]:
            raise ValueError("compute command must not be empty")
        self.command = tuple(command)
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"compute cwd does not exist: {self.cwd}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self.env = dict(env or {})
        self.baseline_command = tuple(baseline_command) if baseline_command else None
        if self.baseline_command is not None and any(not isinstance(item, str) or not item for item in self.baseline_command):
            raise ValueError("baseline_command must contain non-empty strings")
        self.baseline_cwd = Path(baseline_cwd).resolve() if baseline_cwd else self.cwd
        if not self.baseline_cwd.is_dir():
            raise ValueError(f"baseline cwd does not exist: {self.baseline_cwd}")

    async def _run(self, command: Sequence[str], cwd: Path) -> tuple[int | None, str, str, str | None]:
        process = None
        try:
            child_env = os.environ.copy()
            child_env.update(self.env)
            process = await asyncio.create_subprocess_exec(
                *command, cwd=str(cwd), env=child_env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
            return process.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"), None
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return None, "", "", "timeout"
        except (OSError, UnicodeError) as exc:
            return None, "", "", str(exc)

    async def _dependency_preflight(self, command: Sequence[str], cwd: Path) -> dict[str, Any] | None:
        requirements = cwd / "requirements.txt"
        packages = _requirements_packages(requirements)
        if not packages:
            return None
        # Probe the interpreter that will execute the experiment, rather than
        # the API server's interpreter. This matters when the workspace has a
        # project venv or the command uses a different Python installation.
        interpreter = command[0]
        probe_code = (
            "import importlib.util, json, sys; "
            f"items = {json.dumps([{'distribution': d, 'import_name': i} for d, i in packages])}; "
            "print(json.dumps({'python': sys.executable, 'missing': [item for item in items "
            "if importlib.util.find_spec(item['import_name']) is None]}))"
        )
        returncode, stdout, stderr, error = await self._run([interpreter, "-c", probe_code], cwd)
        if error or returncode != 0:
            return {
                "requirements_path": str(requirements),
                "packages": [d for d, _ in packages],
                "missing": [d for d, _ in packages],
                "probe_error": error or stderr[-500:] or "dependency probe failed",
            }
        try:
            result = json.loads(stdout.strip().splitlines()[-1])
            missing = result.get("missing", []) if isinstance(result, dict) else []
            return {
                "requirements_path": str(requirements),
                "packages": [d for d, _ in packages],
                "missing": [item.get("distribution") for item in missing if isinstance(item, dict)],
                "python": result.get("python") if isinstance(result, dict) else None,
            }
        except (json.JSONDecodeError, IndexError, AttributeError):
            return {
                "requirements_path": str(requirements),
                "packages": [d for d, _ in packages],
                "missing": [d for d, _ in packages],
                "probe_error": "dependency probe returned invalid JSON",
            }

    async def _install_dependencies(self, command: Sequence[str], cwd: Path, request: dict[str, Any]) -> dict[str, Any]:
        requirements = Path(request["requirements_path"])
        returncode, stdout, stderr, error = await self._run(
            [command[0], "-m", "pip", "install", "-r", str(requirements)], cwd,
        )
        return {
            "approved": True,
            "requirements_path": str(requirements),
            "returncode": returncode,
            "error": error,
            "stdout": stdout,
            "stderr": stderr,
            "installed": error is None and returncode == 0,
        }

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        command = self.baseline_command if message.action == "run_baseline" and self.baseline_command else self.command
        cwd = self.baseline_cwd if message.action == "run_baseline" else self.cwd
        return await self._artifact(message, task, command, cwd, "local")

    async def _artifact(self, message: A2AMessage, task: ResearchTask, command: Sequence[str], cwd: Path, executor: str) -> Artifact:
        task.runtime = {"phase": "starting", "iteration": message.parameters.get("iteration"), "replicate": message.parameters.get("replicate"), "command": list(command), "cwd": str(cwd), "started_at": datetime.now(timezone.utc).isoformat(), "last_output": None}
        if executor == "local":
            dependency_request = await self._dependency_preflight(command, cwd)
            if dependency_request and dependency_request.get("missing"):
                if message.parameters.get("dependency_approval") is not True:
                    task.runtime = {**task.runtime, "phase": "awaiting_dependency_approval", "dependency_request": dependency_request}
                    return Artifact(kind="ExperimentRun", producer=self.name, inputs=message.input_artifacts,
                                    status="requires_approval", payload={
                                        "executor": executor,
                                        "command": list(command),
                                        "cwd": str(cwd),
                                        "metrics": {},
                                        "metrics_status": "dependency_approval_required",
                                        "dependency_request": dependency_request,
                                    })
                installation = await self._install_dependencies(command, cwd, dependency_request)
                task.runtime = {**task.runtime, "phase": "dependencies_installed" if installation["installed"] else "dependency_install_failed", "dependency_installation": installation}
                if not installation["installed"]:
                    return Artifact(kind="ExperimentRun", producer=self.name, inputs=message.input_artifacts,
                                    status="failed", payload={
                                        "executor": executor,
                                        "command": list(command),
                                        "cwd": str(cwd),
                                        "metrics": {},
                                        "metrics_status": "dependency_install_failed",
                                        "dependency_request": dependency_request,
                                        "dependency_installation": installation,
                                    })
        returncode, stdout, stderr, error = await self._run(command, cwd)
        environment_error = None
        missing = re.search(r"No module named ['\"]([^'\"]+)", stderr)
        if not missing:
            hinted = re.search(r"requires\s+(.+?)\s+and\s+(.+?)(?:;|\.|\s+install)", stderr, re.I)
            if hinted:
                module = hinted.group(1).strip().split()[0]
                missing = type("Missing", (), {"group": lambda self, n: module})()
        if missing:
            module = missing.group(1).split('.')[0]
            probe = await self._run(["python", "-m", "pip", "show", module], cwd)
            environment_error = {
                "missing_module": module,
                "pip_available": bool(probe[1].strip()),
                "remediation": f"Install dependency in the experiment environment: python -m pip install {module}",
            }
        metrics, metrics_status = extract_result(stdout, task.objective_metric)
        status = "created" if returncode == 0 and error is None and metrics is not None else "failed"
        task.runtime = {**task.runtime, "phase": "finished" if status == "created" else "failed", "finished_at": datetime.now(timezone.utc).isoformat(), "last_output": (stdout or stderr)[-1000:] or None}
        return Artifact(kind="ExperimentRun", producer=self.name, inputs=message.input_artifacts, status=status, payload={
            "executor": executor,
            "iteration": message.parameters.get("iteration"),
            "replicate": message.parameters.get("replicate"),
            "run_role": "baseline" if message.action == "run_baseline" else "candidate",
            "command": list(command),
            "cwd": str(cwd),
            "returncode": returncode,
            "timeout_seconds": self.timeout_seconds,
            "error": error,
            "environment_error": environment_error,
            "stdout": stdout,
            "stderr": stderr,
            "metrics": metrics or {},
            "metrics_status": metrics_status,
            "environment": _environment(),
            "request": {"task_id": message.task_id, "action": message.action, "question": task.question, "input_artifacts": message.input_artifacts, "parameters": message.parameters},
        })


class AutoComputeAgent:
    """Choose the experiment entry point after Coding Agent finishes."""
    name = "compute"
    capabilities = ("run_experiment", "collect_metrics", "auto_discover")

    def __init__(self, cwd: str | os.PathLike[str]):
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"compute cwd does not exist: {self.cwd}")

    def _contract(self, message: A2AMessage) -> tuple[list[str], Path] | None:
        for record in reversed(message.input_artifact_data):
            payload = record.get("payload", {}) if isinstance(record, Mapping) else {}
            contract = payload.get("execution_contract")
            if isinstance(contract, Mapping) and isinstance(contract.get("command"), list):
                command = [str(x) for x in contract["command"] if str(x)]
                cwd = Path(contract.get("cwd") or self.cwd).resolve()
                if command and cwd.is_dir():
                    return command, cwd
        for name in ("experiment.py", "candidate.py", "run_experiment.py"):
            if (self.cwd / name).is_file():
                command = ["python", name]
                # Generated runners may declare required argparse inputs. For
                # --external-test, only pass an actual dataset path (never a
                # Python unit-test module). A missing dataset is reported
                # explicitly instead of silently running an invalid command.
                if name == "run_experiment.py":
                    candidates = [self.cwd / "external_test.npz", self.cwd / "external"]
                    candidates += [self.cwd / "data" / "external_test.npz", self.cwd / "data" / "external"]
                    dataset = next((p for p in candidates if p.exists()), None)
                    if dataset is None:
                        # Do not assume a particular project layout: generated
                        # agents often place the held-out set in a nested
                        # artifact/data directory.
                        nested = list(self.cwd.rglob("external_test.npz"))
                        nested += [p for p in self.cwd.rglob("external") if p.is_dir()]
                        dataset = next((p for p in nested if p.exists()), None)
                    text = (self.cwd / name).read_text(encoding="utf-8", errors="ignore")
                    if "--external-test" in text:
                        if dataset is None:
                            return ["__missing_external_test__", name], self.cwd
                        command += ["--external-test", str(dataset)]
                return command, self.cwd
        return None

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        selected = self._contract(message)
        if not selected:
            return Artifact(kind="ExperimentRun", producer=self.name, status="failed", inputs=message.input_artifacts,
                            payload={"executor": "auto", "error": "Coding Agent did not provide an execution contract and no experiment entry point was found", "metrics": {}, "metrics_status": "missing_command"})
        command, cwd = selected
        if command[0] == "__missing_external_test__":
            return Artifact(kind="ExperimentRun", producer=self.name, status="failed", inputs=message.input_artifacts,
                            payload={"executor": "auto", "command": command[1:], "cwd": str(cwd), "error": "run_experiment.py requires --external-test, but no external_test.npz or external/ dataset was found in the Coding workspace", "metrics": {}, "metrics_status": "missing_external_test"})
        return await LocalComputeAgent(command, cwd).handle(message, task)


class DockerComputeAgent(LocalComputeAgent):
    """Run the same experiment contract in a Docker container with explicit limits."""

    name = "compute"
    capabilities = ("run_experiment", "collect_metrics", "isolated_execution")

    def __init__(self, image: str, command: Sequence[str], cwd: str | os.PathLike[str], timeout_seconds: int = 900, cpus: float = 2.0, memory: str = "4g", baseline_command: Sequence[str] | None = None, baseline_cwd: str | os.PathLike[str] | None = None) -> None:
        if not image or any(char in image for char in "\r\n"):
            raise ValueError("docker image must be a non-empty single line")
        if cpus <= 0:
            raise ValueError("cpus must be positive")
        if not memory or any(char in memory for char in "\r\n"):
            raise ValueError("memory must be a non-empty single line")
        super().__init__(command, cwd, timeout_seconds, baseline_command=baseline_command, baseline_cwd=baseline_cwd)
        self.image = image
        self.cpus = cpus
        self.memory = memory

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        selected = self.baseline_command if message.action == "run_baseline" and self.baseline_command else self.command
        selected_cwd = self.baseline_cwd if message.action == "run_baseline" else self.cwd
        docker_command = [
            "docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m", "--cpus", str(self.cpus),
            "--memory", self.memory, "--pids-limit", "256", "-v", f"{selected_cwd}:/workspace:ro",
            "-w", "/workspace", self.image, *selected,
        ]
        artifact = await self._artifact(message, task, docker_command, selected_cwd, "docker")
        payload = dict(artifact.payload)
        payload["image"] = self.image
        payload["limits"] = {"cpus": self.cpus, "memory": self.memory, "network": "none", "pids": 256, "read_only": True, "capabilities": "dropped"}
        return Artifact(kind=artifact.kind, producer=artifact.producer, inputs=artifact.inputs, status=artifact.status, payload=payload)
