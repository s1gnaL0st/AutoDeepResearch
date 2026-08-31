from __future__ import annotations

import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import Artifact, ResearchTask
from .protocol import A2AMessage, ResearchAgent


PROTOCOL_VERSION = "1.0"
AGENT_CARD_PATH = "/.well-known/agent-card.json"


class A2AError(RuntimeError):
    pass


def agent_card(base_url: str, agent: ResearchAgent, description: str | None = None) -> dict[str, Any]:
    return {
        "name": agent.name,
        "description": description or f"AutoResearch adapter for {agent.name}",
        "supportedInterfaces": [{"url": base_url, "protocolBinding": "HTTP+JSON", "protocolVersion": PROTOCOL_VERSION}],
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {"id": capability, "name": capability.replace("_", " "), "description": f"AutoResearch capability: {capability}", "tags": ["autoresearch", capability]}
            for capability in agent.capabilities
        ],
    }


def _read_json(url: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json", "A2A-Version": PROTOCOL_VERSION}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, method=method, data=data, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class A2AHttpAgent:
    """A ResearchAgent adapter for a remote A2A HTTP+JSON endpoint.

    This implementation uses structured-data Parts and carries AutoResearch-specific
    fields in the part body. Its peer must expose the same extension contract.
    """

    capabilities = ("remote_a2a",)

    def __init__(self, name: str, endpoint_url: str, card_url: str | None = None) -> None:
        parsed = urlparse(endpoint_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("endpoint_url must be an absolute HTTP(S) URL")
        self.name = name
        self.endpoint_url = endpoint_url.rstrip("/")
        self.card_url = card_url or f"{parsed.scheme}://{parsed.netloc}{AGENT_CARD_PATH}"
        self._card: dict[str, Any] | None = None

    async def discover(self) -> dict[str, Any]:
        card = await asyncio.to_thread(_read_json, self.card_url)
        interfaces = card.get("supportedInterfaces", [])
        if not interfaces:
            raise A2AError("remote Agent Card has no supported interfaces")
        if not any(interface.get("protocolBinding") == "HTTP+JSON" for interface in interfaces):
            raise A2AError("remote Agent Card does not advertise HTTP+JSON")
        self._card = card
        return card

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        if self._card is None:
            await self.discover()
        request = {
            "message": {
                "messageId": str(uuid.uuid4()),
                "contextId": message.task_id,
                "taskId": message.task_id,
                "role": "ROLE_USER",
                "parts": [{"data": {
                    "action": message.action,
                    "question": task.question,
                    "researchTaskId": message.task_id,
                    "inputArtifacts": message.input_artifacts,
                    "inputArtifactData": message.input_artifact_data,
                    "parameters": message.parameters,
                }}],
            },
            "configuration": {"acceptedOutputModes": ["application/json"], "returnImmediately": False},
        }
        response = await asyncio.to_thread(_read_json, self.endpoint_url + "/message:send", "POST", request)
        if not isinstance(response, dict):
            raise A2AError("remote A2A response must be a JSON object")
        status_data = response.get("status", {})
        if not isinstance(status_data, dict):
            raise A2AError("remote A2A response status must be an object")
        state = status_data.get("state")
        if state not in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"}:
            raise A2AError(f"remote A2A task did not complete: {state or 'unknown'}")
        remote_artifacts = response.get("artifacts", [])
        if not isinstance(remote_artifacts, list):
            raise A2AError("remote A2A artifacts must be an array")
        for remote_artifact in remote_artifacts:
            if not isinstance(remote_artifact, dict):
                raise A2AError("remote A2A artifact entry must be an object")
            parts = remote_artifact.get("parts", [])
            if not isinstance(parts, list):
                raise A2AError("remote A2A artifact parts must be an array")
            for part in parts:
                if not isinstance(part, dict):
                    raise A2AError("remote A2A part must be an object")
                data = part.get("data")
                if isinstance(data, dict) and {"kind", "payload", "producer"}.issubset(data):
                    if (
                        not isinstance(data["kind"], str)
                        or not isinstance(data["payload"], dict)
                        or not isinstance(data["producer"], str)
                        or not isinstance(data.get("inputs", []), list)
                        or not all(isinstance(item, str) for item in data.get("inputs", []))
                    ):
                        raise A2AError("remote Artifact has invalid field types")
                    # Recompute the hash locally instead of trusting a remote
                    # peer's claimed digest before adding it to our provenance graph.
                    claimed_hash = data.get("content_hash", "")
                    artifact = Artifact(
                        kind=data["kind"], payload=data["payload"], producer=data["producer"],
                        inputs=data.get("inputs", []), artifact_id=data.get("artifact_id", str(uuid.uuid4())),
                        schema_version=data.get("schema_version", "0.1"), created_at=data.get("created_at", ""),
                        content_hash="", status=data.get("status", "created"),
                    )
                    if claimed_hash and claimed_hash != artifact.content_hash:
                        raise A2AError("remote Artifact content hash mismatch")
                    return artifact
        if state == "TASK_STATE_FAILED":
            metadata = response.get("metadata", {})
            error = metadata.get("error", "remote A2A task failed") if isinstance(metadata, dict) else "remote A2A task failed"
            return Artifact(
                kind="A2ATaskFailure",
                producer=self.name,
                inputs=message.input_artifacts,
                status="failed",
                payload={
                    "provider": "a2a",
                    "endpoint": self.endpoint_url,
                    "state": state,
                    "error": str(error),
                    "response": response,
                },
            )
        raise A2AError("remote A2A response contained no AutoResearch Artifact")


class A2AAgentServer:
    """Loopback-only A2A HTTP+JSON development server for one ResearchAgent."""

    def __init__(self, agent: ResearchAgent, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("development server only permits a loopback host")
        self.agent = agent
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("server has not been started")
        return f"http://{self.host}:{self._server.server_address[1]}"

    def start(self) -> "A2AAgentServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                content = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("A2A-Version", PROTOCOL_VERSION)
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self) -> None:
                if self.path == AGENT_CARD_PATH:
                    self._send(200, agent_card(outer.base_url, outer.agent))
                else:
                    self._send(404, {"error": "not_found"})

            def do_POST(self) -> None:
                if self.path != "/message:send":
                    self._send(404, {"error": "not_found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    request = json.loads(self.rfile.read(length).decode("utf-8"))
                    message = request["message"]
                    part = next(part["data"] for part in message["parts"] if isinstance(part.get("data"), dict))
                    research_task = ResearchTask(part["question"], task_id=part.get("researchTaskId") or message.get("taskId") or str(uuid.uuid4()))
                    local_message = A2AMessage(
                        research_task.task_id, "remote-a2a-client", outer.agent.name, part["action"],
                        part.get("inputArtifacts", []), part.get("inputArtifactData", []), part.get("parameters", {}),
                    )
                    artifact = asyncio.run(outer.agent.handle(local_message, research_task))
                    response = {
                        "id": research_task.task_id,
                        "contextId": message.get("contextId") or research_task.task_id,
                        "status": {"state": "TASK_STATE_COMPLETED" if artifact.status == "created" else "TASK_STATE_FAILED"},
                        "artifacts": [{
                            "artifactId": artifact.artifact_id,
                            "name": artifact.kind,
                            "description": artifact.producer,
                            "parts": [{"data": artifact.to_dict(), "mediaType": "application/json"}],
                        }],
                    }
                    self._send(200, response)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send(400, {"error": "invalid_request", "message": str(exc)})
                except Exception as exc:  # A2A task failure, not a server crash.
                    self._send(200, {"id": str(uuid.uuid4()), "status": {"state": "TASK_STATE_FAILED"}, "metadata": {"error": str(exc)}})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> "A2AAgentServer":
        return self.start()

    def __exit__(self, *args: object) -> None:
        self.close()
