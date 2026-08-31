import asyncio
import json
from urllib.request import Request, urlopen
from pathlib import Path
from unittest.mock import AsyncMock

from autoresearch.compute import LocalComputeAgent, _requirements_packages
from autoresearch.models import ResearchTask
from autoresearch.protocol import A2AMessage
from autoresearch.api import ResearchApiServer
from autoresearch.models import ResearchState
from autoresearch.queue import JobRecord
from autoresearch.storage import ArtifactStore


def _message(task: ResearchTask, approved: bool | None = None) -> A2AMessage:
    parameters = {"iteration": 1, "replicate": 1}
    if approved is not None:
        parameters["dependency_approval"] = approved
    return A2AMessage(task.task_id, "control-plane", "compute", "run_experiment", [], [], parameters)


def test_requirements_parser_maps_common_distribution_names(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("torch>=2.3\ntorchvision==0.18\nscikit-learn\nPillow\n# comment\n", encoding="utf-8")

    assert _requirements_packages(requirements) == [
        ("torch", "torch"),
        ("torchvision", "torchvision"),
        ("scikit-learn", "sklearn"),
        ("Pillow", "PIL"),
    ]


def test_local_compute_returns_dependency_approval_request_without_running(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("package_that_is_not_installed_anywhere\n", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = ["python", "-c", f"from pathlib import Path; Path(r'{marker}').write_text('ran') ; print('{{\"metrics\":{{\"score\":0.9}}}}')"]
    task = ResearchTask("dependency gate")
    agent = LocalComputeAgent(command, tmp_path)

    artifact = asyncio.run(agent.handle(_message(task), task))

    assert artifact.status == "requires_approval"
    assert artifact.payload["metrics_status"] == "dependency_approval_required"
    assert artifact.payload["dependency_request"]["missing"] == ["package_that_is_not_installed_anywhere"]
    assert task.runtime["phase"] == "awaiting_dependency_approval"
    assert not marker.exists()


def test_approved_dependency_installation_runs_experiment_after_install(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("package_that_is_not_installed_anywhere\n", encoding="utf-8")
    task = ResearchTask("approved dependency gate")
    agent = LocalComputeAgent(["python", "-c", "print('{\"metrics\":{\"score\":0.9}}')"], tmp_path)
    agent._install_dependencies = AsyncMock(return_value={
        "approved": True,
        "installed": True,
        "requirements_path": str(tmp_path / "requirements.txt"),
        "returncode": 0,
        "error": None,
        "stdout": "installed",
        "stderr": "",
    })

    artifact = asyncio.run(agent.handle(_message(task, approved=True), task))

    assert artifact.status == "created"
    assert artifact.payload["metrics"] == {"score": 0.9}
    agent._install_dependencies.assert_awaited_once()


def test_api_dependency_denial_keeps_task_resumable(tmp_path: Path) -> None:
    with ResearchApiServer(str(tmp_path)) as server:
        task = ResearchTask("api dependency denial", state=ResearchState.AWAITING_DEPENDENCY_APPROVAL)
        task.execution_status = "awaiting_dependency_approval"
        task.runtime = {"dependency_request": {"missing": ["torch"], "requirements_path": "requirements.txt"}}
        ArtifactStore(tmp_path).put_task(task)
        request = Request(
            server.base_url + f"/research/{task.task_id}/resume",
            method="POST",
            data=b'{"approve_dependencies": false}',
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            result = json.loads(response.read())

    assert result["task"]["state"] == ResearchState.AWAITING_DEPENDENCY_APPROVAL.value
    assert result["task"]["execution_status"] == "paused"
    assert "用户拒绝" in result["task"]["error"]
    assert result["paused"] is True


def test_api_dependency_approval_clears_visible_gate_before_queueing(tmp_path: Path) -> None:
    with ResearchApiServer(str(tmp_path)) as server:
        task = ResearchTask("api dependency approval", state=ResearchState.AWAITING_DEPENDENCY_APPROVAL)
        task.execution_status = "awaiting_dependency_approval"
        task.runtime = {
            "phase": "awaiting_dependency_approval",
            "dependency_request": {"missing": ["torch"], "requirements_path": "requirements.txt"},
        }
        ArtifactStore(tmp_path).put_task(task)
        # The HTTP action is under test, not background workflow execution.
        server._runner.submit = lambda task_id, execute, max_attempts: JobRecord("queued-job", task_id)
        request = Request(
            server.base_url + f"/research/{task.task_id}/resume",
            method="POST",
            data=b'{"approve_dependencies": true}',
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            result = json.loads(response.read())

    assert result["task"]["state"] == ResearchState.IMPLEMENTING.value
    assert result["task"]["runtime"]["dependency_approval"] is True
    assert result["task"]["runtime"]["phase"] == "dependency_install_approved"
