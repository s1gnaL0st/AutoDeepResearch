from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import statistics

from .models import Artifact, ResearchTask
from .protocol import A2AMessage
from .literature import FixtureLiteratureSource, LiteratureSource, candidate_passages, deduplicate
from .fulltext import extract_local_full_text
from .rag import PostgresRAGStore, RAGIndex, configured_embedder


class FakeLiteratureAgent:
    name = "fake-literature"
    capabilities = ("search_literature", "verify_citations")

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        return Artifact(
            kind="EvidenceSet",
            producer=self.name,
            payload={
                "query": task.question,
                "sources": [{"title": "Reproducible baseline study", "doi": "10.0000/example", "support": "baseline evidence"}],
                "source_level": "synthetic",
            },
        )


class LiteratureAgent:
    """Collects source snapshots and returns a deduplicated, citation-tagged EvidenceSet."""

    name = "literature"
    capabilities = ("search_literature", "verify_citations")

    def __init__(self, sources: list[LiteratureSource] | None = None, limit_per_source: int = 5, fulltext_paths: list[str] | None = None) -> None:
        self.sources = sources or [FixtureLiteratureSource()]
        self.limit_per_source = limit_per_source
        self.fulltext_paths = list(fulltext_paths or [])

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        results = await asyncio.gather(*(source.search(task.question, self.limit_per_source) for source in self.sources))
        records = deduplicate([record for result in results for record in result.records])
        passages = [passage for record in records for passage in candidate_passages(record, task.question)]
        fulltext_documents = [extract_local_full_text(path) for path in self.fulltext_paths]
        fulltext_passages = [passage for document in fulltext_documents for passage in document.get("passages", [])]
        intelligence = [result.intelligence for result in results if result.intelligence]
        for brief in intelligence:
            for card in brief.get("paper_cards", []):
                paper_key = "title:" + str(card.get("title", "")).casefold()
                for evidence in card.get("full_text_evidence", []):
                    text = evidence["text"].strip()
                    fulltext_passages.append({
                        "passage_id": f"deerflow-{hashlib.sha256((paper_key + text).encode()).hexdigest()[:16]}",
                        "paper_key": paper_key,
                        "text": text,
                        "locator": evidence["locator"],
                        "source": "deerflow",
                        "source_url": card.get("url"),
                        "passage_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "support_status": "candidate_unverified",
                        "verification_scope": "deerflow_full_text_excerpt",
                    })
        rag_documents = [{
            "document_id": passage.get("document_id") or passage.get("paper_key") or passage.get("passage_id"),
            "text": passage.get("text", ""),
            "locator": passage.get("locator", {}),
            "metadata": {"source": passage.get("source"), "source_url": passage.get("source_url"), "passage_id": passage.get("passage_id")},
        } for passage in fulltext_passages if isinstance(passage.get("text"), str) and passage["text"].strip()]
        rag_backend = "in_memory"
        embedder, embedding_model, embedding_error = configured_embedder()
        if rag_documents:
            dsn = os.environ.get("AUTORESEARCH_DATABASE_URL")
            if dsn:
                try:
                    rag = PostgresRAGStore(dsn, embedder=embedder)
                    indexed = rag.index_documents(rag_documents)
                    retrieved = rag.search(task.question)
                    rag_backend = "postgres_pgvector_ready" if getattr(rag, "vector_available", False) else "postgres_jsonb_compat"
                except Exception as exc:
                    # Retrieval must not make an otherwise valid literature
                    # result fail; preserve the exact fallback reason.
                    rag = RAGIndex(embedder)
                    indexed = sum(rag.add_document(item["document_id"], item["text"], item["locator"], item["metadata"]) for item in rag_documents)
                    retrieved = rag.search(task.question)
                    rag_backend = f"in_memory_fallback:{type(exc).__name__}"
            else:
                rag = RAGIndex(embedder)
                indexed = sum(rag.add_document(item["document_id"], item["text"], item["locator"], item["metadata"]) for item in rag_documents)
                retrieved = rag.search(task.question)
        else:
            indexed, retrieved = 0, []
        return Artifact(kind="EvidenceSet", producer=self.name, payload={
            "query": task.question,
            "records": [record.to_dict() for record in records],
            "passages": passages,
            "full_text_documents": fulltext_documents,
            "full_text_passages": fulltext_passages,
            "rag": {
                "backend": rag_backend,
                "indexed_chunk_count": indexed,
                "retrieved_chunks": [item.to_dict() for item in retrieved],
                "embedding_model": embedding_model,
                "embedding_configuration_error": embedding_error,
                "retrieval_policy": "0.7 vector cosine + 0.3 lexical overlap; retrieved chunks remain unverified evidence candidates",
            },
            "literature_intelligence": intelligence,
            "source_snapshots": [result.snapshot() for result in results],
            "summary": {
                "source_count": len(results),
                "successful_sources": sum(result.status == "success" for result in results),
                "record_count_before_dedup": sum(len(result.records) for result in results),
                "record_count": len(records),
                "candidate_passage_count": len(passages),
                "full_text_document_count": len(fulltext_documents),
                "full_text_passage_count": len(fulltext_passages),
                "rag_retrieved_chunk_count": len(retrieved),
                "metadata_verified_doi_count": sum(record.citation_status == "metadata_verified" and record.doi is not None for record in records),
                "literature_intelligence_count": len(intelligence),
            },
        }, status="created" if records or fulltext_passages or intelligence else "insufficient_evidence")


class FakeHypothesisAgent:
    name = "hypothesis"
    capabilities = ("generate_hypotheses", "revise_hypotheses")

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        # Consume the planner contract when available.  The fixture agent is
        # intentionally simple, but it should exercise the same data path as
        # an external hypothesis agent rather than silently ignoring the plan.
        plan_record = next((item for item in message.input_artifact_data if item.get("kind") == "ResearchPlan"), None)
        plan = (plan_record or {}).get("payload", {}).get("plans", [])
        plan = plan[0] if plan and isinstance(plan[0], dict) else {}
        metric = plan.get("metric") or "score"
        candidate = plan.get("candidate") or "a reproducible intervention"
        baseline = plan.get("baseline") or "the baseline"
        failure = plan.get("failure_condition") or "no improvement over the baseline"
        return Artifact(kind="HypothesisSet", producer=self.name, inputs=message.input_artifacts, payload={
            "hypotheses": [{
                "id": "H1",
                "statement": f"{candidate} improves {metric} relative to {baseline} for: {task.question}",
                "metric": metric,
                "test_criterion": f"Reject the candidate if {failure}",
                "rationale": "Derived from the Literature Intelligence research plan.",
            }],
            "research_plan_used": plan,
        })


class ClaimEvidenceAgent:
    """Build a conservative claim-to-passage candidate map from full text.

    Lexical overlap is only a triage signal. The artifact deliberately never
    upgrades a passage to semantic, causal, or citation-level verification.
    """

    name = "evidence"
    capabilities = ("map_claims_to_evidence",)

    @staticmethod
    def _terms(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]{3,}", value.casefold()))

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        records = {record.get("kind"): record for record in message.input_artifact_data}
        evidence = records.get("EvidenceSet")
        hypotheses = records.get("HypothesisSet")
        if evidence is None or hypotheses is None:
            return Artifact(kind="ClaimEvidenceMap", producer=self.name, inputs=message.input_artifacts, status="failed", payload={"error": "EvidenceSet and HypothesisSet are required"})
        evidence_payload = evidence.get("payload", {})
        passages = list(evidence_payload.get("full_text_passages", []))
        # RAG candidates are admissible retrieval evidence, but remain
        # explicitly unverified.  Include them in lexical claim triage so a
        # relevant retrieved chunk cannot be silently ignored downstream.
        for passage in evidence_payload.get("rag", {}).get("retrieved_chunks", []):
            if isinstance(passage, dict) and passage.get("text"):
                passages.append({
                    **passage,
                    "support_status": "rag_candidate_unverified",
                    "verification_scope": "rag_retrieved_chunk",
                })
        maps = []
        for hypothesis in hypotheses.get("payload", {}).get("hypotheses", []):
            statement = hypothesis.get("statement") if isinstance(hypothesis, dict) else None
            if not isinstance(statement, str):
                continue
            terms = self._terms(statement)
            candidates = []
            for passage in passages:
                text = passage.get("text")
                if not isinstance(text, str):
                    continue
                overlap = sorted(terms.intersection(self._terms(text)))
                if overlap:
                    candidates.append({
                        "passage_id": passage.get("passage_id"),
                        "document_id": passage.get("document_id"),
                        "passage_sha256": passage.get("passage_sha256"),
                        "locator": passage.get("locator"),
                        "overlap_terms": overlap,
                        "score": len(overlap),
                        "support_status": passage.get("support_status", "lexical_candidate_unverified"),
                        "verification_required": "human_entailment_and_citation_review",
                    })
            candidates.sort(key=lambda candidate: (-candidate["score"], str(candidate["passage_id"])))
            maps.append({
                "claim_id": hypothesis.get("id"),
                "claim_text": statement,
                "mapping_status": "lexical_candidates_found" if candidates else "no_full_text_candidate",
                "candidates": candidates[:5],
                "verified_support": False,
            })
        matching_claims = sum(1 for item in maps if item["candidates"])
        return Artifact(kind="ClaimEvidenceMap", producer=self.name, inputs=message.input_artifacts, payload={
            "claims": maps,
            "summary": {
                "claim_count": len(maps),
                "full_text_document_count": len(evidence.get("payload", {}).get("full_text_documents", [])),
                "full_text_passage_count": len(passages),
                "claims_with_lexical_candidates": matching_claims,
                "verified_claim_count": 0,
                "requires_human_entailment_review": True,
            },
            "limitations": [
                "Lexical overlap is retrieval triage, not semantic entailment.",
                "No claim is marked verified by this automated mapper.",
            ],
        })


class FakeCodingAgent:
    name = "coding"
    capabilities = ("inspect_repo", "implement_experiment", "run_tests")

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        plan_record = next((item for item in message.input_artifact_data if item.get("kind") == "ResearchPlan"), None)
        plan = (plan_record or {}).get("payload", {}).get("plans", [])
        plan = plan[0] if plan and isinstance(plan[0], dict) else {}
        return Artifact(kind="CodeRevision", producer=self.name, inputs=message.input_artifacts, payload={
            "commit": "fake0001", "files_changed": ["experiment.py"], "tests": {"passed": True},
            "research_plan_used": {
                key: plan.get(key) for key in ("baseline", "candidate", "metric", "failure_condition", "resource_budget") if plan.get(key) is not None
            },
        })


class FakeComputeAgent:
    name = "compute"
    capabilities = ("run_experiment",)

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        return Artifact(kind="ExperimentRun", producer=self.name, inputs=message.input_artifacts, payload={
            "executor": "fake",
            "run_id": "baseline-0001" if message.action == "run_baseline" else "run-0001",
            "iteration": message.parameters.get("iteration"),
            "replicate": message.parameters.get("replicate"),
            "run_role": "baseline" if message.action == "run_baseline" else "candidate",
            "metrics": {"score": 0.81}, "environment": "synthetic",
            "command": None, "cwd": None, "returncode": None,
            "metrics_status": "synthetic"
        })


class FakeAnalysisAgent:
    name = "analysis"
    capabilities = ("analyze_results", "map_claims_to_evidence")

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        return Artifact(kind="Finding", producer=self.name, inputs=message.input_artifacts, payload={
            "finding": "The intervention improved the synthetic score.", "effect": 0.81, "confidence": "illustrative"
        })


class FakeReviewerAgent:
    name = "reviewer"
    capabilities = ("review", "attempt_falsification")

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        return Artifact(kind="ReviewReport", producer=self.name, inputs=message.input_artifacts, payload={
            "decision": "pass_with_human_review", "reproducibility": ["synthetic execution only"], "blocking_issues": []
        })


class MetricsAnalysisAgent:
    """Produces descriptive findings from recorded metrics, without inferring causality."""

    name = "analysis"
    capabilities = ("analyze_results", "map_claims_to_evidence")

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        runs = [record for record in message.input_artifact_data if record.get("kind") == "ExperimentRun"]
        candidate_runs = [record for record in runs if record.get("payload", {}).get("run_role") != "baseline" and record.get("status") == "created"]
        run = candidate_runs[-1] if candidate_runs else None
        baseline = next((record for record in runs if record.get("payload", {}).get("run_role") == "baseline"), None)
        if run is None or run.get("status") != "created":
            return Artifact(kind="Finding", producer=self.name, inputs=message.input_artifacts, status="failed", payload={"error": "missing successful ExperimentRun"})
        metrics = run.get("payload", {}).get("metrics", {})
        if not metrics:
            return Artifact(kind="Finding", producer=self.name, inputs=message.input_artifacts, status="failed", payload={"error": "ExperimentRun has no metrics"})
        values_by_metric: dict[str, list[float]] = {}
        for record in candidate_runs:
            for key, value in record.get("payload", {}).get("metrics", {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                    values_by_metric.setdefault(key, []).append(float(value))
        summary = {}
        for key, values in values_by_metric.items():
            mean = statistics.fmean(values)
            stdev = statistics.stdev(values) if len(values) > 1 else 0.0
            n = len(values)
            standard_error = stdev / math.sqrt(n) if n else 0.0
            # Normal approximation is intentionally labelled descriptive; it
            # is not a substitute for a domain-appropriate inferential test.
            margin = 1.96 * standard_error if n > 1 else None
            summary[key] = {
                "mean": mean, "sample_stddev": stdev,
                "standard_error": standard_error, "n": n,
                "confidence_level": 0.95 if n > 1 else None,
                "confidence_interval_95": [mean - margin, mean + margin] if margin is not None else None,
            }
        means = {key: values["mean"] for key, values in summary.items()}
        payload = {
            "finding": "Observed descriptive statistics from the recorded experiment replicates.",
            "metrics": means or metrics,
            "statistics": summary,
            "replicate_artifact_ids": [record["artifact_id"] for record in candidate_runs],
            "experiment_artifact_id": run["artifact_id"],
            "confidence": "descriptive_only",
            "limitations": ["Descriptive uncertainty is estimated from sample standard deviation; no inferential test was supplied.", "This finding does not establish causality."],
        }
        if baseline and baseline.get("status") == "created":
            baseline_metrics = baseline.get("payload", {}).get("metrics", {})
            delta = {
                key: means[key] - float(baseline_metrics[key])
                for key in means.keys() & baseline_metrics.keys()
                if isinstance(baseline_metrics[key], (int, float)) and not isinstance(baseline_metrics[key], bool)
            }
            payload["baseline_artifact_id"] = baseline["artifact_id"]
            payload["baseline_metrics"] = baseline_metrics
            payload["delta_vs_baseline"] = delta
            payload["baseline_comparison"] = {"candidate_means": means, "baseline_metrics": baseline_metrics}
            payload["effect_size"] = {key: delta[key] for key in delta}
            payload["limitations"] = ["Baseline comparison is descriptive; confidence intervals use a normal approximation and no inferential test was supplied.", "This finding does not establish causality."]
        return Artifact(kind="Finding", producer=self.name, inputs=message.input_artifacts, payload=payload)


class ResearchCriticAgent:
    """Emit a bounded decision after each experiment analysis."""
    name = "critic"
    capabilities = ("critique_results", "adapt_hypothesis")

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        finding = next((a for a in reversed(message.input_artifact_data) if a.get("kind") == "Finding"), {})
        delta = finding.get("payload", {}).get("delta_vs_baseline", {})
        meaningful = any(isinstance(v, (int, float)) and abs(v) > 1e-9 for v in delta.values())
        if task.baseline_artifact_id and meaningful:
            # A measurable improvement satisfies the current hypothesis;
            # stop early instead of spending the remaining iteration budget.
            decision = "stop_early"
        elif task.baseline_artifact_id and not meaningful:
            # No improvement is feedback for the next hypothesis/code round.
            decision = "revise_hypothesis"
        else:
            decision = "continue_iteration" if task.iteration < task.max_iterations else "stop_budget_exhausted"
        return Artifact(kind="ResearchDecision", producer=self.name, inputs=message.input_artifacts, payload={
            "decision": decision, "iteration": task.iteration,
            "hypothesis_action": "generate_alternative" if decision == "revise_hypothesis" else "retain_and_test",
            "reason": "Feedback is bounded by max_iterations and passed to the next coding iteration.",
        })


class EvidenceReviewAgent:
    """Checks evidence and experiment presence; it deliberately requires human scientific review."""

    name = "reviewer"
    capabilities = ("review", "attempt_falsification")

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        findings = [record for record in message.input_artifact_data if record.get("kind") == "Finding"]
        runs = [record for record in message.input_artifact_data if record.get("kind") == "ExperimentRun"]
        maps = [record for record in message.input_artifact_data if record.get("kind") == "ClaimEvidenceMap"]
        decisions = [record for record in message.input_artifact_data if record.get("kind") == "ResearchDecision"]
        finding = findings[-1] if findings else None
        claim_map = maps[-1] if maps else None
        candidate_runs = [record for record in runs if record.get("payload", {}).get("run_role") != "baseline"]
        issues = []
        if not candidate_runs:
            issues.append("No candidate ExperimentRun was provided.")
        elif any(run.get("status") != "created" for run in candidate_runs):
            issues.append("At least one candidate replicate failed.")
        if finding is None:
            issues.append("No Finding was provided.")
        elif finding.get("payload", {}).get("confidence") != "descriptive_only":
            issues.append("Finding confidence must be conservative until statistical validation exists.")
        limitations = ["Automated review cannot establish novelty or causal validity."]
        if claim_map is None:
            limitations.append("No claim-to-evidence map was supplied for review.")
        elif claim_map.get("payload", {}).get("summary", {}).get("verified_claim_count", 0) == 0:
            limitations.append("No claim has automated semantic verification; full-text candidates require human entailment review.")
        # Keep reviewer contexts logically independent: each lens receives the
        # same immutable artifacts but emits its own findings before a small
        # control-plane synthesis. This is local and cheap enough for a PC,
        # while preserving the contract needed by remote A2A reviewers later.
        reviewer_lenses = (
            ("methodology", "technical_soundness", "Check design, baselines, leakage and reproducibility."),
            ("evidence", "evidence_quality", "Check claim-to-source support and conclusion boundaries."),
            ("significance", "originality", "Check novelty/significance claims and missing comparisons."),
        )
        independent_reviews = [{
            "reviewer_id": reviewer_id,
            "focus": focus,
            "instruction": instruction,
            "decision": "blocked" if issues else "requires_human_review",
            "blocking_issues": list(issues),
        } for reviewer_id, focus, instruction in reviewer_lenses]
        return Artifact(kind="ReviewReport", producer=self.name, inputs=message.input_artifacts, payload={
            "decision": "requires_human_review" if not issues else "blocked",
            "blocking_issues": issues,
            "independent_reviews": independent_reviews,
            "synthesis": {
                "reviewer_count": len(independent_reviews),
                "agreement": "all_lenses_agree_on_blocking_issues" if issues else "all_lenses_require_human_review",
                "revision_required": True,
            },
            "reproducibility": ["Artifact hashes and input references are validated by the control plane.", f"candidate_replicates={len(candidate_runs)}"],
            "feedback_decision": decisions[-1].get("payload", {}) if decisions else {"status": "not_provided"},
            "scientific_limitations": limitations,
        }, status="created" if not issues else "failed")


class ReportWriterAgent:
    """Build a conservative, provenance-linked final report from prior artifacts."""

    name = "report"
    capabilities = ("write_report", "summarize_findings")

    async def handle(self, message: A2AMessage, task: ResearchTask) -> Artifact:
        records = {record.get("kind"): record for record in message.input_artifact_data}
        evidence = records.get("EvidenceSet")
        hypothesis = records.get("HypothesisSet")
        runs = [record for record in message.input_artifact_data if record.get("kind") == "ExperimentRun"]
        candidate_runs = [record for record in runs if record.get("payload", {}).get("run_role") != "baseline"]
        run = candidate_runs[-1] if candidate_runs else records.get("ExperimentRun")
        finding = records.get("Finding")
        claim_map = records.get("ClaimEvidenceMap")
        adjudication = records.get("EvidenceAdjudication")
        review = records.get("ReviewReport")
        decision = records.get("ResearchDecision")
        package = records.get("ReproducibilityPackage")
        plan_record = records.get("ResearchPlan")
        plan_payload = (plan_record or {}).get("payload", {})
        plans = plan_payload.get("plans", []) if isinstance(plan_payload, dict) else []
        research_plan = plans[0] if plans and isinstance(plans[0], dict) else {}
        evidence_payload = (evidence or {}).get("payload", {}) or {}
        evidence_records = evidence_payload.get("records", []) or []
        snapshots = evidence_payload.get("source_snapshots", []) or []
        # Fixture literature is intentionally deterministic demo data. Keep
        # this provenance visible even when compute execution is real.
        fixture_evidence = (
            evidence_payload.get("source_level") == "synthetic"
            or any(isinstance(record, dict) and record.get("source") == "fixture" for record in evidence_records)
            or any(isinstance(snapshot, dict) and snapshot.get("source") == "fixture" for snapshot in snapshots)
        )
        evidence_mode = "synthetic_fixture" if fixture_evidence else "live_or_configured"
        # Do not infer real execution merely from the selected profile. Older
        # FakeCompute artifacts did not include an environment marker, so the
        # presence of a metric alone is not evidence that code ran. A run is
        # synthetic when it is explicitly fake/synthetic or has the legacy
        # empty execution fields. Real runs must carry an executor and command
        # output/return code (local, docker, or a remote adapter payload).
        def is_synthetic(run_record: dict) -> bool:
            payload = run_record.get("payload", {}) or {}
            if payload.get("executor") == "fake" or payload.get("environment") == "synthetic":
                return True
            if not payload.get("command") and payload.get("returncode") is None and not payload.get("stdout"):
                return payload.get("executor") in (None, "")
            return False

        # A task can contain failed attempts or legacy Fake runs after a
        # retry.  Do not let those historical records taint a later successful
        # real execution.  Classify from successful candidate runs first.
        successful_runs = [item for item in candidate_runs if item.get("status") == "created"]
        classified_runs = successful_runs or candidate_runs or runs
        has_real = any(not is_synthetic(item) for item in classified_runs)
        has_synthetic = any(is_synthetic(item) for item in classified_runs)
        execution_mode = (
            "real_configured" if has_real else
            "synthetic_demo" if has_synthetic else
            "unknown"
        )
        if not all(record is not None for record in (evidence, hypothesis, run, finding, review, package)):
            return Artifact(kind="ResearchReport", producer=self.name, inputs=message.input_artifacts, status="failed", payload={
                "error": "report requires EvidenceSet, HypothesisSet, ExperimentRun, Finding, ReviewReport and ReproducibilityPackage",
            })
        return Artifact(kind="ResearchReport", producer=self.name, inputs=message.input_artifacts, payload={
            "title": f"AutoResearch report: {task.question}",
            "question": task.question,
            "execution_mode": execution_mode,
            "evidence_mode": evidence_mode,
            "research_plan": research_plan,
            "objective": {"metric": task.objective_metric, "direction": task.objective_direction, "best_iteration": task.best_iteration, "best_value": task.best_value},
            "executive_summary": (
                f"{len(candidate_runs)} candidate experiment run(s) were executed for the question "
                f"‘{task.question}’ and reviewed. "
                + ("This is synthetic demo output; configure a real Coding Agent and experiment command before interpreting it."
                   if execution_mode == "synthetic_demo" else
                   "Results are descriptive and require human scientific validation.")
                + (" Literature uses the deterministic fixture source; configure live literature retrieval before making evidence claims."
                   if evidence_mode == "synthetic_fixture" else "")
            ),
            "hypotheses": hypothesis["payload"].get("hypotheses", []),
            "evidence": {
                "record_count": evidence["payload"].get("summary", {}).get("record_count", 0),
                "candidate_passage_count": evidence["payload"].get("summary", {}).get("candidate_passage_count", 0),
                "artifact_id": evidence["artifact_id"],
            },
            "claim_evidence": {
                "artifact_id": claim_map["artifact_id"] if claim_map else None,
                "summary": claim_map["payload"].get("summary", {}) if claim_map else {"status": "not_generated"},
                "claims": claim_map["payload"].get("claims", []) if claim_map else [],
            },
            "evidence_adjudication": {
                "artifact_id": adjudication["artifact_id"] if adjudication else None,
                "summary": adjudication["payload"].get("summary", {}) if adjudication else {"status": "not_adjudicated"},
                "decisions": adjudication["payload"].get("decisions", []) if adjudication else [],
            },
            "experiment": {"metrics": finding["payload"].get("metrics", run["payload"].get("metrics", {})), "statistics": finding["payload"].get("statistics", {}), "artifact_id": run["artifact_id"], "run_count": len(candidate_runs), "trajectory": [{"artifact_id": item["artifact_id"], "iteration": item["payload"].get("iteration"), "replicate": item["payload"].get("replicate"), "metrics": item["payload"].get("metrics", {}), "status": item["status"]} for item in runs]},
            "finding": {"text": finding["payload"].get("finding"), "confidence": finding["payload"].get("confidence"), "artifact_id": finding["artifact_id"]},
            "review": {"decision": review["payload"].get("decision"), "artifact_id": review["artifact_id"]},
            "feedback_loop": {
                "artifact_id": decision["artifact_id"] if decision else None,
                "decision": decision["payload"] if decision else {"status": "not_run"},
            },
            "reproducibility_artifact_id": package["artifact_id"],
            "limitations": [
                "Candidate abstract passages and lexical full-text matches are not claim-level semantic verification.",
                "Repeated runs without a baseline, uncertainty estimates or statistical testing cannot establish causality.",
                "Novelty and scientific validity require human review.",
            ],
            "report_status": "draft_for_human_review",
            "writing_profile": {
                "style": "nature_family_evidence_first",
                "sections": ["abstract", "introduction", "related_work", "methods", "experiments", "discussion", "limitations", "references"],
                "rules": [
                    "Every quantitative claim must point to an ExperimentRun or Finding Artifact.",
                    "Unsupported novelty, causality and mechanism claims remain explicitly qualified.",
                    "Missing design facts are reported as AUTHOR_INPUT_NEEDED rather than invented.",
                ],
            },
            "review_profile": {
                "style": "nature_reviewer",
                "independent_reviewers": 3,
                "axes": ["originality", "scientific_importance", "technical_soundness", "evidence_quality", "readability"],
                "status": "three_local_lenses_with_control_plane_synthesis; external_A2A_adapter_supported",
            },
            "statistics_profile": {
                "report_effect_sizes": True,
                "report_uncertainty": True,
                "independent_unit_required": True,
                "inferential_tests": "AUTHOR_INPUT_NEEDED",
            },
        })
