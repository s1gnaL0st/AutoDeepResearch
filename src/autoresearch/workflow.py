from __future__ import annotations

from .models import Artifact, ResearchState, ResearchTask
from .protocol import A2AMessage, ResearchAgent
from .storage import ArtifactStore
from .reproducibility import build_reproducibility_package


class WorkflowCancelled(RuntimeError):
    pass

class WorkflowPaused(RuntimeError):
    pass


class WorkflowDependencyApprovalRequired(RuntimeError):
    pass


class ResearchWorkflow:
    _EXPECTED_ARTIFACT_KINDS = {
        "literature": "EvidenceSet",
        "hypothesis": "HypothesisSet",
        "evidence": "ClaimEvidenceMap",
        "coding": "CodeRevision",
        "compute": "ExperimentRun",
        "analysis": "Finding",
        "critic": "ResearchDecision",
        "reviewer": "ReviewReport",
        "report": "ResearchReport",
    }

    def __init__(self, store: ArtifactStore, agents: dict[str, ResearchAgent]) -> None:
        self.store = store
        self.agents = agents

    def _agent(self, stage: str) -> ResearchAgent:
        """Resolve canonical stage names while accepting old persisted aliases."""
        aliases = {
            "literature": ("literature", "fake-literature"),
            "hypothesis": ("hypothesis", "fake-hypothesis"),
            "evidence": ("evidence", "claim-evidence"),
            "coding": ("coding", "fake-coding", "command-coding"),
            "compute": ("compute", "fake-compute", "command-compute", "docker-compute"),
            "analysis": ("analysis", "fake-analysis", "metrics-analysis"),
            "critic": ("critic",),
            "reviewer": ("reviewer", "fake-reviewer", "evidence-reviewer"),
            "report": ("report", "fake-report", "report-writer"),
        }
        for name in aliases.get(stage, (stage,)):
            if name in self.agents:
                return self.agents[name]
        raise KeyError(f"no agent configured for stage {stage!r}")

    def _check_cancellation(self, task: ResearchTask) -> None:
        """Synchronize a durable cancellation request at every agent boundary."""
        try:
            task.cancel_requested = task.cancel_requested or self.store.get_task(task.task_id).cancel_requested
        except FileNotFoundError:
            # A brand-new synchronous task is persisted by the first completed
            # stage; it cannot yet have been targeted by the control-plane API.
            pass
        if task.cancel_requested:
            raise WorkflowCancelled("cancel requested")
        if task.pause_requested:
            task.paused_from_state = task.state
            task.state = ResearchState.PAUSED
            self.store.put_task(task)
            raise WorkflowPaused("pause requested")

    async def _run_after_approval(self, task: ResearchTask, call) -> ResearchTask:
        if task.max_iterations < 1 or task.max_iterations > 20:
            raise ValueError("max_iterations must be between 1 and 20")
        if task.replicates < 1 or task.replicates > 20:
            raise ValueError("replicates must be between 1 and 20")
        if not task.objective_metric or task.objective_direction not in {"max", "min"}:
            raise ValueError("objective_metric must be non-empty and objective_direction must be 'max' or 'min'")
        if task.baseline_requested and not task.baseline_artifact_id:
            task.transition(ResearchState.BASELINING)
            baseline = await call("compute", "run_baseline")
            task.baseline_artifact_id = baseline.artifact_id
            self.store.put_task(task)
            if baseline.status != "created":
                raise RuntimeError("baseline experiment failed")
            task.transition(ResearchState.IMPLEMENTING)
        elif task.state == ResearchState.AWAITING_APPROVAL:
            task.transition(ResearchState.IMPLEMENTING)

        for iteration in range(task.iteration + 1, task.max_iterations + 1):
            task.iteration = iteration
            self.store.put_task(task)
            if iteration > 1:
                task.transition(ResearchState.IMPLEMENTING)
            coding_inputs = None
            context_ids = []
            for kind in ("ResearchPlan", "HypothesisSet", "ResearchDecision", "Finding", "ExperimentRun"):
                artifact_id = next(
                    (candidate for candidate in reversed(task.artifacts)
                     if self.store.get_artifact(candidate).get("kind") == kind),
                    None,
                )
                if artifact_id:
                    context_ids.append(artifact_id)
            if context_ids:
                coding_inputs = list(reversed(context_ids))
            code = await call("coding", "implement_experiment", coding_inputs)
            task.transition(ResearchState.RUNNING)
            iteration_runs = []
            for replicate in range(1, task.replicates + 1):
                # Keep the failed ExperimentRun available as an auditable
                # repair input instead of letting the generic call wrapper
                # abort before Coding Agent can inspect it.
                run = await call(
                    "compute",
                    "run_experiment",
                    parameters={"replicate": replicate},
                    allow_failed=True,
                )
                if run.status != "created":
                    # Give Coding Agent one bounded repair opportunity using
                    # the concrete Compute failure (stderr, traceback and
                    # environment details), then rerun this replicate.
                    task.runtime = {
                        **task.runtime,
                        "phase": "coding_repair",
                        "repair_attempt": 1,
                        "repair_artifact_id": run.artifact_id,
                    }
                    self.store.put_task(task)
                    repaired = await call("coding", "repair_experiment", [code.artifact_id, run.artifact_id])
                    code = repaired
                    task.runtime = {**task.runtime, "phase": "retrying_experiment"}
                    self.store.put_task(task)
                    run = await call(
                        "compute",
                        "run_experiment",
                        parameters={"replicate": replicate, "repair_attempt": 1},
                        allow_failed=True,
                    )
                    if run.status != "created":
                        raise RuntimeError("experiment failed after one Coding Agent repair attempt")
                iteration_runs.append(run.artifact_id)
            self._update_best(task, iteration_runs)
            task.transition(ResearchState.ANALYZING)
            analysis_inputs = [code.artifact_id, *iteration_runs]
            if task.baseline_artifact_id:
                analysis_inputs.insert(0, task.baseline_artifact_id)
            analysis = await call("analysis", "analyze_results", analysis_inputs)
            # A feedback Artifact is useful only when another iteration is
            # available; preserving the one-shot MVP artifact contract keeps
            # existing single-iteration runs backward compatible.
            stop_early = False
            if task.max_iterations > 1:
                decision = await call("critic", "critique_results", [analysis.artifact_id])
                stop_early = decision.payload.get("decision") == "stop_early"
                if decision.payload.get("decision") == "revise_hypothesis" and iteration < task.max_iterations:
                    evidence_id = next(
                        (artifact_id for artifact_id in reversed(task.artifacts)
                         if self.store.get_artifact(artifact_id).get("kind") == "EvidenceSet"),
                        None,
                    )
                    if evidence_id:
                        hypotheses = await call(
                            "hypothesis", "revise_hypotheses",
                            [evidence_id, analysis.artifact_id, decision.artifact_id],
                            parameters={"feedback_action": "revise_hypothesis"},
                        )
                        if any(name in self.agents for name in ("evidence", "claim-evidence")):
                            await call("evidence", "map_claims_to_evidence", [evidence_id, hypotheses.artifact_id])
            if stop_early:
                # Remain in ANALYZING; the enclosing transition to REVIEWING
                # records that the bounded research loop ended deliberately.
                break
            if iteration < task.max_iterations:
                task.transition(ResearchState.ITERATING)
        task.transition(ResearchState.REVIEWING)
        review_inputs = self._latest_iteration_run_ids(task)
        latest_finding = next(
            (artifact_id for artifact_id in reversed(task.artifacts) if self.store.get_artifact(artifact_id).get("kind") == "Finding"),
            None,
        )
        if latest_finding:
            review_inputs.append(latest_finding)
        latest_map = next(
            (artifact_id for artifact_id in reversed(task.artifacts) if self.store.get_artifact(artifact_id).get("kind") == "ClaimEvidenceMap"),
            None,
        )
        if latest_map:
            review_inputs.append(latest_map)
        latest_decision = next(
            (artifact_id for artifact_id in reversed(task.artifacts)
             if self.store.get_artifact(artifact_id).get("kind") == "ResearchDecision"),
            None,
        )
        if latest_decision:
            review_inputs.append(latest_decision)
        if task.baseline_artifact_id and task.baseline_artifact_id not in review_inputs:
            review_inputs.insert(0, task.baseline_artifact_id)
        await call("reviewer", "review", review_inputs)
        package = build_reproducibility_package(task, self.store)
        self.store.put_artifact(package)
        task.artifacts.append(package.artifact_id)
        self.store.put_task(task)
        if package.status != "validated":
            raise RuntimeError("reproducibility package failed integrity validation")
        await call("report", "write_report", list(task.artifacts))
        task.transition(ResearchState.REPORT_READY)
        return task

    def _update_best(self, task: ResearchTask, run_ids: list[str]) -> None:
        """Update the objective ledger from the current iteration mean."""
        values = []
        for artifact_id in run_ids:
            run = self.store.get_artifact(artifact_id)
            metrics = run.get("payload", {}).get("metrics", {})
            if task.objective_metric == "auto" and isinstance(metrics, dict):
                numeric = [key for key, item in metrics.items() if isinstance(item, (int, float)) and not isinstance(item, bool)]
                if numeric:
                    # Infer once from the first successful run and persist the
                    # selected metric for subsequent iterations and reporting.
                    task.objective_metric = (
                        "accuracy" if "accuracy" in numeric else
                        "score" if "score" in numeric else numeric[0]
                    )
            value = metrics.get(task.objective_metric) if isinstance(metrics, dict) else None
            if run.get("kind") == "ExperimentRun" and run.get("status") == "created" and isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(float(value))
        if not values:
            return
        value = sum(values) / len(values)
        better = task.best_value is None or (value > task.best_value if task.objective_direction == "max" else value < task.best_value)
        if better:
            task.best_value = value
            task.best_iteration = task.iteration
        self.store.put_task(task)

    def _latest_iteration_run_ids(self, task: ResearchTask) -> list[str]:
        """Return every candidate replicate from the last completed iteration."""
        run_ids = []
        for artifact_id in task.artifacts:
            record = self.store.get_artifact(artifact_id)
            payload = record.get("payload", {})
            if (
                record.get("kind") == "ExperimentRun"
                and payload.get("run_role") != "baseline"
                and payload.get("iteration") == task.iteration
            ):
                run_ids.append(artifact_id)
        return run_ids

    async def run(self, task: ResearchTask, auto_approve: bool = True) -> ResearchTask:
        try:
            async def call(
                agent_name: str,
                action: str,
                input_artifacts: list[str] | None = None,
                parameters: dict | None = None,
                allow_failed: bool = False,
            ):
                self._check_cancellation(task)
                inputs = list(task.artifacts[-2:] if input_artifacts is None else input_artifacts)
                input_data = [self.store.get_artifact(artifact_id) for artifact_id in inputs]
                message_parameters = {
                    "iteration": task.iteration,
                    "max_iterations": task.max_iterations,
                    "replicate": None,
                    "replicates": task.replicates,
                    "objective_metric": task.objective_metric,
                    "objective_direction": task.objective_direction,
                    "best_iteration": task.best_iteration,
                    "best_value": task.best_value,
                    "dependency_approval": task.runtime.get("dependency_approval", False),
                }
                if parameters:
                    message_parameters.update(parameters)
                message = A2AMessage(
                    task.task_id, "control-plane", agent_name, action, inputs, input_data,
                    message_parameters,
                )
                artifact = await self._agent(agent_name).handle(message, task)
                expected_kind = self._EXPECTED_ARTIFACT_KINDS[agent_name]
                if artifact.kind != expected_kind:
                    # Do not allow a malformed or misrouted external Agent
                    # response to satisfy a later stage by accident. Retain a
                    # compact immutable audit record instead of persisting it
                    # as a successful stage Artifact.
                    # Persist the original response first. This is essential
                    # for remote A2A failures: the control-plane contract
                    # violation explains why execution stopped, while the
                    # returned Artifact retains peer-specific diagnostics.
                    self.store.put_artifact(artifact)
                    task.artifacts.append(artifact.artifact_id)
                    violation = Artifact(
                        kind="AgentContractViolation",
                        producer="control-plane",
                        inputs=[*inputs, artifact.artifact_id],
                        status="failed",
                        payload={
                            "stage": agent_name,
                            "expected_kind": expected_kind,
                            "returned_kind": artifact.kind,
                            "returned_status": artifact.status,
                            "returned_producer": artifact.producer,
                            "returned_artifact": artifact.to_dict(),
                        },
                    )
                    self.store.put_artifact(violation)
                    task.artifacts.append(violation.artifact_id)
                    self.store.put_task(task)
                    raise RuntimeError(
                        f"{agent_name} returned {artifact.kind}; expected {expected_kind}"
                    )
                self.store.put_artifact(artifact)
                task.artifacts.append(artifact.artifact_id)
                if artifact.status == "requires_approval":
                    request = artifact.payload.get("dependency_request", {})
                    task.runtime = {**task.runtime, "phase": "awaiting_dependency_approval", "dependency_request": request}
                    task.error = "实验需要安装依赖，等待用户批准"
                    # Re-running the whole iteration after approval is
                    # deliberate: the generated workspace may have changed
                    # while the dependency gate was pending, so coding and
                    # compute must share one fresh, auditable attempt.
                    task.iteration = max(0, task.iteration - 1)
                    if task.state != ResearchState.AWAITING_DEPENDENCY_APPROVAL:
                        task.transition(ResearchState.AWAITING_DEPENDENCY_APPROVAL)
                    task.execution_status = "awaiting_dependency_approval"
                    self.store.put_task(task)
                    raise WorkflowDependencyApprovalRequired(task.error)
                self._check_cancellation(task)
                self.store.put_task(task)
                if artifact.status in {"failed", "insufficient_evidence"} and not allow_failed:
                    raise RuntimeError(f"{agent_name} returned artifact status {artifact.status}")
                return artifact

            if task.state == ResearchState.AWAITING_APPROVAL:
                if not auto_approve:
                    return task
                await self._run_after_approval(task, call)
            elif task.state == ResearchState.IMPLEMENTING:
                # A retry after coding/compute failure keeps the existing
                # evidence and hypotheses; resume at implementation instead
                # of repeating the expensive literature search.
                await self._run_after_approval(task, call)
            elif task.state == ResearchState.DRAFT:
                task.transition(ResearchState.SEARCHING)
                # A retried task retains prior Artifacts for provenance, but a
                # fresh literature search must not treat an old failed attempt
                # as research input.
                evidence = await call("literature", "search_literature", [])
                if evidence.status == "insufficient_evidence":
                    raise RuntimeError("literature search produced no evidence; inspect source snapshots")
                intelligence = evidence.payload.get("literature_intelligence", [])
                intelligence_artifact_id = None
                plan_artifact_id = None
                if intelligence:
                    intelligence_artifact = Artifact(
                        kind="LiteratureIntelligence", producer="literature-intelligence",
                        inputs=[evidence.artifact_id], payload={
                            "query": task.question,
                            "briefs": intelligence,
                            "paper_cards": [card for brief in intelligence for card in brief.get("paper_cards", [])],
                            "comparison_matrix": [row for brief in intelligence for row in brief.get("comparison_matrix", [])],
                            "gap_candidates": [gap for brief in intelligence for gap in brief.get("gap_candidates", [])],
                            "research_plans": [brief.get("research_plan", {}) for brief in intelligence],
                            "status": "candidate_for_human_review",
                        },
                    )
                    self.store.put_artifact(intelligence_artifact)
                    task.artifacts.append(intelligence_artifact.artifact_id)
                    intelligence_artifact_id = intelligence_artifact.artifact_id
                    plans = [plan for plan in intelligence_artifact.payload.get("research_plans", []) if plan]
                    if plans:
                        plan_artifact = Artifact(
                            kind="ResearchPlan", producer="research-planner",
                            inputs=[intelligence_artifact.artifact_id], payload={
                                "query": task.question,
                                "plans": plans,
                                "status": "candidate_for_human_review",
                                "resource_policy": "single PC or ordinary server; bounded experiments",
                            },
                        )
                        self.store.put_artifact(plan_artifact)
                        task.artifacts.append(plan_artifact.artifact_id)
                        plan_artifact_id = plan_artifact.artifact_id
                    self.store.put_task(task)
                task.transition(ResearchState.EVIDENCE_READY)
                hypothesis_inputs = [evidence.artifact_id]
                if intelligence_artifact_id:
                    hypothesis_inputs.append(intelligence_artifact_id)
                if plan_artifact_id:
                    hypothesis_inputs.append(plan_artifact_id)
                hypotheses = await call("hypothesis", "generate_hypotheses", hypothesis_inputs)
                if any(name in self.agents for name in ("evidence", "claim-evidence")):
                    await call("evidence", "map_claims_to_evidence", [evidence.artifact_id, hypotheses.artifact_id])
                task.transition(ResearchState.HYPOTHESES_READY)
                task.transition(ResearchState.AWAITING_APPROVAL)
                if not auto_approve:
                    self.store.put_task(task)
                    return task
                await self._run_after_approval(task, call)
            elif task.state in {ResearchState.REPORT_READY, ResearchState.CANCELLED}:
                # Terminal tasks are idempotent reads: a repeated API/CLI call
                # must not rewrite a completed report or revive a cancellation.
                return task
            else:
                raise ValueError(f"task cannot be run from state {task.state}")
        except WorkflowCancelled as exc:
            if task.state != ResearchState.CANCELLED:
                task.transition(ResearchState.CANCELLED)
            task.error = str(exc)
        except WorkflowDependencyApprovalRequired:
            # This is a resumable human gate, not a failed experiment.
            self.store.put_task(task)
            return task
        except Exception as exc:
            task.error = str(exc)
            if task.state not in {ResearchState.FAILED, ResearchState.CANCELLED, ResearchState.REPORT_READY}:
                task.transition(ResearchState.FAILED)
        self.store.put_task(task)
        return task
