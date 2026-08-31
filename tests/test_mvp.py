import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from autoresearch.cli import build_agents, build_execution_profile, parse_command, resume_research, run_research
from autoresearch.models import Artifact, ResearchState, ResearchTask
from autoresearch.agents import ClaimEvidenceAgent, EvidenceReviewAgent, FakeCodingAgent, LiteratureAgent, MetricsAnalysisAgent, ReportWriterAgent
from autoresearch.literature import DeepResearchSource, DeerFlowSource, FixtureLiteratureSource, PaperRecord, SourceSearch, candidate_passages, deduplicate, normalize_doi
from autoresearch.protocol import A2AMessage
from autoresearch.coding import ClaudeCodeAgent, SubprocessCodingAgent
from autoresearch.compute import DockerComputeAgent, LocalComputeAgent, extract_result
from autoresearch.a2a import A2AAgentServer, A2AHttpAgent, A2AError
from autoresearch.reproducibility import build_reproducibility_package
from autoresearch.fulltext import extract_local_full_text
from autoresearch.rag import RAGIndex, chunk_text
from autoresearch.adjudication import build_evidence_adjudication
from autoresearch.hypothesis import SubprocessHypothesisAgent
from autoresearch.external_agents import SubprocessAnalysisAgent, SubprocessReviewerAgent
from autoresearch.storage import ArtifactStore
from autoresearch.workflow import ResearchWorkflow
from autoresearch.api import ResearchApiServer
from autoresearch.queue import BackgroundTaskRunner
from autoresearch.agents import FakeAnalysisAgent, FakeCodingAgent, FakeComputeAgent, FakeHypothesisAgent, FakeReviewerAgent


class MvpTests(unittest.TestCase):
    def test_rag_chunking_and_hybrid_retrieval(self):
        self.assertGreaterEqual(len(chunk_text("signal " * 300, chunk_size=100, overlap=20)), 2)
        index = RAGIndex()
        index.add_document("paper-a", "Kernel methods improve handwritten digit classification accuracy.", {"type": "abstract"})
        index.add_document("paper-b", "Marine biology observations describe coral reef growth.", {"type": "abstract"})
        hits = index.search("handwritten digit classification with kernels", top_k=1)
        self.assertEqual(hits[0].document_id, "paper-a")
        self.assertGreater(hits[0].lexical_score, 0)

    def test_claim_mapper_includes_rag_candidates(self):
        evidence = Artifact("EvidenceSet", {"full_text_passages": [], "rag": {"retrieved_chunks": [{"chunk_id": "c1", "document_id": "d1", "text": "The intervention improves transfer accuracy.", "locator": {"page": 2}}]}}, "literature")
        hypotheses = Artifact("HypothesisSet", {"hypotheses": [{"id": "H1", "statement": "The intervention improves transfer accuracy"}]}, "hypothesis")
        result = asyncio.run(ClaimEvidenceAgent().handle(A2AMessage("t", "control", "evidence", "map_claims_to_evidence", [evidence.artifact_id, hypotheses.artifact_id], [evidence.to_dict(), hypotheses.to_dict()]), ResearchTask("q")))
        self.assertEqual(result.payload["summary"]["claims_with_lexical_candidates"], 1)
        self.assertEqual(result.payload["claims"][0]["candidates"][0]["support_status"], "rag_candidate_unverified")
    def test_existing_experiment_is_auto_wired_when_workspace_is_configured(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "experiment.py"), "w", encoding="utf-8") as handle:
                handle.write("print('{\"metrics\": {\"score\": 0.93}}')\n")
            agents = build_agents(coding_agent="fake", coding_cwd=root)
            self.assertEqual(agents["compute"].name, "compute")
            profile = build_execution_profile(coding_cwd=root)
            self.assertEqual(profile["compute_command"], ["python", "experiment.py"])
            self.assertEqual(profile["compute_cwd"], os.path.realpath(root))

    def test_existing_experiment_produces_real_run_instead_of_fake_compute(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "experiment.py"), "w", encoding="utf-8") as handle:
                handle.write("print('{\"metrics\": {\"score\": 0.93}}')\n")
            store_root = os.path.join(root, "store")
            task = asyncio.run(run_research("existing code", store_root, coding_cwd=root))
            store = ArtifactStore(store_root)
            runs = [store.get_artifact(aid) for aid in task.artifacts if store.get_artifact(aid).get("kind") == "ExperimentRun"]
            self.assertEqual(runs[0]["payload"]["metrics"]["score"], 0.93)
            self.assertIsInstance(runs[0]["payload"].get("environment"), dict)

    def test_fixture_agents_consume_research_plan_contract(self):
        plan = Artifact(kind="ResearchPlan", producer="planner", payload={"plans": [{
            "baseline": "logistic", "candidate": "rbf svm", "metric": "accuracy",
            "failure_condition": "accuracy does not improve", "resource_budget": "2 min",
        }]})
        task = ResearchTask("digits")
        message = A2AMessage("t", "control-plane", "hypothesis", "generate_hypotheses", [plan.artifact_id], [plan.to_dict()])
        hypothesis = asyncio.run(FakeHypothesisAgent().handle(message, task))
        self.assertEqual(hypothesis.payload["hypotheses"][0]["metric"], "accuracy")
        self.assertEqual(hypothesis.payload["research_plan_used"]["candidate"], "rbf svm")
        code_message = A2AMessage("t", "control-plane", "coding", "implement_experiment", [plan.artifact_id], [plan.to_dict()])
        code = asyncio.run(FakeCodingAgent().handle(code_message, task))
        self.assertEqual(code.payload["research_plan_used"]["resource_budget"], "2 min")

    def test_deerflow_structured_literature_intelligence_parser(self):
        raw = '<literature_intelligence>{"paper_cards":[{"title":"A paper","source_level":"full_text","method":"M"}],"gap_candidates":[{"statement":"A candidate gap"}],"research_plan":{"baseline":"B"}}</literature_intelligence>'
        parsed = DeerFlowSource._parse_intelligence([raw])
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["paper_cards"][0]["title"], "A paper")
        self.assertEqual(parsed["gap_candidates"][0]["evidence_level"], "candidate_only")
        self.assertEqual(parsed["research_plan"]["baseline"], "B")

    def test_deerflow_full_text_evidence_flows_into_evidence_set(self):
        class StructuredSource:
            name = "deerflow"

            async def search(self, query, limit):
                brief = {"provider": "deerflow", "paper_cards": [{
                    "title": "A paper", "source_level": "full_text", "url": "https://example.org/paper",
                    "full_text_evidence": [{"text": "A verbatim methods passage.", "locator": {"type": "page", "value": "p. 4"}}],
                }], "comparison_matrix": [], "gap_candidates": [], "research_plan": {}}
                return SourceSearch("deerflow", "command://deerflow", [PaperRecord("A paper", [], 2024, url="https://example.org/paper", source="deerflow")], "now", "raw", intelligence=brief)

        artifact = asyncio.run(LiteratureAgent([StructuredSource()]).handle(A2AMessage("t", "test", "literature", "search_literature"), ResearchTask("digits")))
        passages = artifact.payload["full_text_passages"]
        self.assertEqual(artifact.kind, "EvidenceSet")
        self.assertEqual(len(passages), 1)
        self.assertEqual(passages[0]["locator"]["value"], "p. 4")
        self.assertEqual(passages[0]["support_status"], "candidate_unverified")

    def test_literature_intelligence_artifact_is_persisted_and_rendered(self):
        class StructuredLiterature:
            name = "literature"
            capabilities = ("search_literature",)
            async def handle(self, message, task):
                return Artifact(kind="EvidenceSet", producer=self.name, inputs=message.input_artifacts, payload={
                    "records": [{"title": "A paper"}],
                    "literature_intelligence": [{
                        "paper_cards": [{"title": "A paper", "source_level": "full_text", "method": "M", "limitations": []}],
                        "comparison_matrix": [{"dimension": "method", "comparison": "different", "paper_titles": ["A paper"]}],
                        "gap_candidates": [{"statement": "Candidate gap", "evidence_level": "candidate_only"}],
                        "research_plan": {"baseline": "B"},
                    }],
                    "summary": {"record_count": 1},
                })
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            agents = build_agents()
            agents["literature"] = StructuredLiterature()
            task = asyncio.run(ResearchWorkflow(store, agents).run(ResearchTask("structured")))
            records = [store.get_artifact(aid) for aid in task.artifacts]
            from autoresearch.cli import _write_human_report
            _write_human_report(task, root)
            self.assertTrue(os.path.exists(os.path.join(root, "paper_cards.md")))
            self.assertTrue(os.path.exists(os.path.join(root, "innovation_brief.md")))
        intelligence = next(record for record in records if record["kind"] == "LiteratureIntelligence")
        self.assertEqual(intelligence["payload"]["gap_candidates"][0]["statement"], "Candidate gap")

    def test_state_transition_rejects_invalid_edge(self):
        with self.assertRaises(ValueError):
            ResearchTask("q").transition(ResearchState.REPORT_READY)


    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as root:
            task = asyncio.run(run_research("Does X improve Y?", root))
            report = ArtifactStore(root).get_artifact(task.artifacts[-1])
        self.assertEqual(task.state, ResearchState.REPORT_READY)
        self.assertEqual(len(task.artifacts), 9)
        self.assertEqual(report["kind"], "ResearchReport")
        self.assertEqual(report["payload"]["report_status"], "draft_for_human_review")
        self.assertEqual(task.history[-1]["to"], "REPORT_READY")

    def test_multiple_iterations_record_full_experiment_trajectory(self):
        with tempfile.TemporaryDirectory() as root:
            task = asyncio.run(run_research("iterate", root, iterations=3, objective_metric="score", objective_direction="max"))
            store = ArtifactStore(root)
            records = [store.get_artifact(artifact_id) for artifact_id in task.artifacts]
            runs = [record for record in records if record["kind"] == "ExperimentRun"]
            package = next(record for record in records if record["kind"] == "ReproducibilityPackage")
            report = records[-1]
        self.assertEqual(task.state, ResearchState.REPORT_READY)
        self.assertEqual(len(runs), 3)
        self.assertEqual([run["payload"]["iteration"] for run in runs], [1, 2, 3])
        self.assertEqual([item["iteration"] for item in package["payload"]["experiment_trajectory"]], [1, 2, 3])
        self.assertEqual(report["payload"]["experiment"]["run_count"], 3)
        self.assertTrue(any(item["to"] == ResearchState.ITERATING.value for item in task.history))
        self.assertEqual(task.best_iteration, 1)
        self.assertEqual(task.best_value, 0.81)
        self.assertEqual(report["payload"]["objective"]["best_value"], 0.81)
        finding = next(record for record in records if record["kind"] == "Finding")
        self.assertEqual(finding["payload"]["statistics"]["score"]["confidence_level"], 0.95)
        self.assertEqual(len(finding["payload"]["statistics"]["score"]["confidence_interval_95"]), 2)
        decisions = [record for record in records if record["kind"] == "ResearchDecision"]
        self.assertEqual(len(decisions), 3)
        self.assertEqual(report["payload"]["feedback_loop"]["decision"]["decision"], "stop_budget_exhausted")

    def test_non_improving_feedback_regenerates_hypothesis(self):
        with tempfile.TemporaryDirectory() as root:
            baseline = [sys.executable, "-c", "print('{\"metrics\":{\"score\":0.50}}')"]
            candidate = [sys.executable, "-c", "print('{\"metrics\":{\"score\":0.50}}')"]
            task = asyncio.run(run_research(
                "revise hypothesis", root, compute_command=candidate,
                baseline_command=baseline, iterations=2,
            ))
            records = [ArtifactStore(root).get_artifact(artifact_id) for artifact_id in task.artifacts]
        self.assertEqual(task.state, ResearchState.REPORT_READY)
        decisions = [record for record in records if record["kind"] == "ResearchDecision"]
        hypotheses = [record for record in records if record["kind"] == "HypothesisSet"]
        self.assertEqual(decisions[0]["payload"]["decision"], "revise_hypothesis")
        self.assertGreaterEqual(len(hypotheses), 2)

    def test_critic_stop_early_ends_iteration_loop(self):
        class StopCritic:
            name = "critic"
            capabilities = ("critique_results",)

            async def handle(self, message, task):
                return Artifact(
                    kind="ResearchDecision", producer="critic",
                    inputs=message.input_artifacts,
                    payload={"decision": "stop_early", "reason": "test"},
                )

        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            agents = build_agents()
            agents["critic"] = StopCritic()
            task = ResearchTask("stop early", max_iterations=3)
            task = asyncio.run(ResearchWorkflow(store, agents).run(task))
            records = [store.get_artifact(artifact_id) for artifact_id in task.artifacts]
        runs = [record for record in records if record["kind"] == "ExperimentRun"]
        self.assertEqual(task.state, ResearchState.REPORT_READY)
        self.assertEqual(len(runs), 1)
        self.assertFalse(any(item["to"] == ResearchState.ITERATING.value for item in task.history))

    def test_objective_configuration_is_part_of_resume_profile(self):
        with tempfile.TemporaryDirectory() as root:
            paused = asyncio.run(run_research("objective", root, auto_approve=False, objective_metric="loss", objective_direction="min"))
            resumed = asyncio.run(resume_research(paused.task_id, root, objective_metric="loss", objective_direction="min"))
            self.assertEqual(resumed.state, ResearchState.REPORT_READY)
            with self.assertRaises(ValueError):
                asyncio.run(resume_research(paused.task_id, root, objective_metric="score", objective_direction="max"))

    def test_replicates_are_persisted_and_part_of_resume_profile(self):
        with tempfile.TemporaryDirectory() as root:
            paused = asyncio.run(run_research("replicates", root, auto_approve=False, replicates=3))
            resumed = asyncio.run(resume_research(paused.task_id, root, replicates=3))
            self.assertEqual(resumed.state, ResearchState.REPORT_READY)
            self.assertEqual(resumed.replicates, 3)
            with self.assertRaises(ValueError):
                asyncio.run(resume_research(paused.task_id, root, replicates=2))

    def test_baseline_is_compared_and_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            baseline = [sys.executable, "-c", "print('{\"metrics\":{\"score\":0.50}}')"]
            candidate = [sys.executable, "-c", "print('{\"metrics\":{\"score\":0.80}}')"]
            task = asyncio.run(run_research(
                "baseline", root, compute_command=candidate, baseline_command=baseline,
                objective_metric="score", objective_direction="max",
            ))
            store = ArtifactStore(root)
            records = [store.get_artifact(artifact_id) for artifact_id in task.artifacts]
            runs = [record for record in records if record["kind"] == "ExperimentRun"]
            finding = next(record for record in records if record["kind"] == "Finding")
        self.assertEqual(task.state, ResearchState.REPORT_READY)
        self.assertEqual(runs[0]["payload"]["run_role"], "baseline")
        self.assertEqual(runs[1]["payload"]["run_role"], "candidate")
        self.assertEqual(finding["payload"]["delta_vs_baseline"], {"score": 0.30000000000000004})
        self.assertEqual(task.best_value, 0.8)

    def test_multiple_iterations_record_full_experiment_trajectory(self):
        with tempfile.TemporaryDirectory() as root:
            task = asyncio.run(run_research("iterate", root, iterations=3))
            store = ArtifactStore(root)
            runs = [store.get_artifact(artifact_id) for artifact_id in task.artifacts if store.get_artifact(artifact_id)["kind"] == "ExperimentRun"]
            package = store.get_artifact(next(artifact_id for artifact_id in task.artifacts if store.get_artifact(artifact_id)["kind"] == "ReproducibilityPackage"))
            report = store.get_artifact(task.artifacts[-1])
        self.assertEqual(task.state, ResearchState.REPORT_READY)
        self.assertEqual(len(runs), 3)
        self.assertEqual([run["payload"]["iteration"] for run in runs], [1, 2, 3])
        self.assertEqual([item["iteration"] for item in package["payload"]["experiment_trajectory"]], [1, 2, 3])
        self.assertEqual(report["payload"]["experiment"]["run_count"], 3)
        self.assertTrue(any(item["to"] == ResearchState.ITERATING.value for item in task.history))


    def test_deduplicate_prefers_metadata_verified_doi_record(self):
        records = [
            PaperRecord("First", [], 2023, doi="https://doi.org/10.1/ABC", citation_status="unverified"),
            PaperRecord("Second", [], 2024, doi="doi:10.1/abc", citation_status="metadata_verified"),
        ]
        unique = deduplicate(records)
        self.assertEqual(normalize_doi(unique[0].doi), "10.1/abc")
        self.assertEqual(unique[0].title, "Second")


    def test_literature_agent_snapshots_and_deduplicates(self):
        duplicate = PaperRecord("Duplicate", [], 2024, doi="10.0000/example", citation_status="metadata_verified")
        agent = LiteratureAgent([FixtureLiteratureSource([duplicate, duplicate])])
        artifact = asyncio.run(agent.handle(A2AMessage("task", "test", "literature", "search_literature"), ResearchTask("question")))
        self.assertEqual(artifact.kind, "EvidenceSet")
        self.assertEqual(artifact.payload["summary"]["record_count_before_dedup"], 2)
        self.assertEqual(artifact.payload["summary"]["record_count"], 1)
        self.assertTrue(artifact.payload["source_snapshots"][0]["raw_sha256"])

    def test_candidate_passages_are_hashable_and_explicitly_unverified(self):
        record = PaperRecord(
            "Paper", [], 2024, abstract="Causal representation learning improves transfer. It needs evaluation.",
            url="https://example.test/paper", source="fixture",
        )
        passages = candidate_passages(record, "causal transfer")
        self.assertEqual(len(passages), 2)
        self.assertEqual(passages[0]["locator"]["type"], "abstract")
        self.assertEqual(passages[0]["support_status"], "candidate")
        self.assertEqual(len(passages[0]["passage_sha256"]), 64)

    def test_local_fulltext_extraction_preserves_hash_and_locator(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "paper.html")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("<html><body><h2>Methods</h2><p>We evaluate the intervention on a held-out dataset.</p><script>ignore()</script><p>Results remain descriptive.</p></body></html>")
            document = extract_local_full_text(path)
        self.assertEqual(document["status"], "extracted")
        self.assertEqual(document["format"], "html")
        self.assertEqual(len(document["passages"]), 2)
        self.assertEqual(document["passages"][0]["locator"]["section"], "Methods")
        self.assertEqual(document["passages"][0]["support_status"], "candidate")
        self.assertEqual(len(document["content_sha256"]), 64)

    def test_claim_evidence_map_never_marks_lexical_match_verified(self):
        from autoresearch.agents import ClaimEvidenceAgent
        evidence = Artifact("EvidenceSet", {
            "full_text_documents": [{"document_id": "doc-1"}],
            "full_text_passages": [{"passage_id": "p-1", "document_id": "doc-1", "passage_sha256": "a" * 64, "locator": {"type": "html_paragraph"}, "text": "The intervention improves transfer accuracy."}],
        }, "literature")
        hypotheses = Artifact("HypothesisSet", {"hypotheses": [{"id": "H1", "statement": "The intervention improves transfer accuracy."}]}, "hypothesis")
        mapped = asyncio.run(ClaimEvidenceAgent().handle(
            A2AMessage("task", "control", "evidence", "map_claims_to_evidence", [evidence.artifact_id, hypotheses.artifact_id], [evidence.to_dict(), hypotheses.to_dict()]),
            ResearchTask("question"),
        ))
        self.assertEqual(mapped.kind, "ClaimEvidenceMap")
        self.assertEqual(mapped.payload["summary"]["verified_claim_count"], 0)
        self.assertEqual(mapped.payload["claims"][0]["candidates"][0]["support_status"], "lexical_candidate_unverified")
        self.assertFalse(mapped.payload["claims"][0]["verified_support"])

    def test_evidence_adjudication_validates_candidate_membership(self):
        claim_map = Artifact("ClaimEvidenceMap", {
            "claims": [{"claim_id": "H1", "candidates": [{"passage_id": "p-1"}]}]
        }, "evidence")
        adjudication = build_evidence_adjudication(claim_map, [{"claim_id": "H1", "passage_id": "p-1", "decision": "supported", "note": "The passage directly addresses the claim."}], adjudicator="researcher")
        self.assertEqual(adjudication.kind, "EvidenceAdjudication")
        self.assertEqual(adjudication.payload["summary"]["supported_count"], 1)
        self.assertEqual(adjudication.payload["decisions"][0]["verification_status"], "human_adjudicated")
        with self.assertRaises(ValueError):
            build_evidence_adjudication(claim_map, [{"claim_id": "H1", "passage_id": "not-a-candidate", "decision": "supported"}], adjudicator="researcher")

    def test_workflow_attaches_fulltext_and_claim_map_to_report(self):
        with tempfile.TemporaryDirectory() as root:
            paper = os.path.join(root, "paper.txt")
            with open(paper, "w", encoding="utf-8") as handle:
                handle.write("The intervention improves the target metric.\n\nThis is a descriptive evaluation.")
            store_root = os.path.join(root, "store")
            task = asyncio.run(run_research("target metric", store_root, fulltext_paths=[paper]))
            store = ArtifactStore(store_root)
            evidence = next(store.get_artifact(item) for item in task.artifacts if store.get_artifact(item)["kind"] == "EvidenceSet")
            claim_map = next(store.get_artifact(item) for item in task.artifacts if store.get_artifact(item)["kind"] == "ClaimEvidenceMap")
            report = store.get_artifact(task.artifacts[-1])
        self.assertEqual(task.state, ResearchState.REPORT_READY)
        self.assertEqual(evidence["payload"]["summary"]["full_text_passage_count"], 2)
        self.assertEqual(claim_map["payload"]["summary"]["verified_claim_count"], 0)
        self.assertEqual(report["payload"]["claim_evidence"]["artifact_id"], claim_map["artifact_id"])

    def test_deepresearch_source_validates_structured_records(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-c", "import json,sys; r=json.load(sys.stdin); print(json.dumps({'records':[{'title':r['query'],'authors':['A'], 'year':2025, 'doi':'10.1/demo'}]}))"]
            source = DeepResearchSource(command, root)
            result = asyncio.run(source.search("structured query", 5))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.records[0].doi, "10.1/demo")
        self.assertEqual(result.records[0].source, "deepresearch")

    def test_workflow_can_use_deepresearch_source(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-c", "import json,sys; r=json.load(sys.stdin); print(json.dumps({'papers':[{'title':r['query'], 'authors':[], 'year':2025, 'abstract':'The question has an abstract.'}]}))"]
            task = asyncio.run(run_research(
                "external research", root, literature_mode="deepresearch",
                deepresearch_command=command, auto_approve=False,
            ))
            evidence = ArtifactStore(root).get_artifact(task.artifacts[0])
        self.assertEqual(task.state, ResearchState.AWAITING_APPROVAL)
        self.assertEqual(evidence["payload"]["records"][0]["title"], "external research")

    def test_deerflow_source_extracts_explicit_citations_from_ndjson(self):
        with tempfile.TemporaryDirectory() as root:
            script = "import json,sys; print(json.dumps({'type':'messages-tuple','data':{'content':'Answer [citation:Paper A](https://example.test/a)'}})); print(json.dumps({'type':'end','data':{}}))"
            source = DeerFlowSource([sys.executable, "-c", script], root)
            result = asyncio.run(source.search("deerflow question", 5))
        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].title, "Paper A")
        self.assertEqual(result.records[0].url, "https://example.test/a")
        self.assertTrue(result.snapshot()["raw_sha256"])

    def test_workflow_can_use_deerflow_source(self):
        with tempfile.TemporaryDirectory() as root:
            script = "import json,sys; print(json.dumps({'type':'messages-tuple','data':{'content':'Answer [citation:Paper A](https://example.test/a)'}}))"
            task = asyncio.run(run_research(
                "deerflow research", root, literature_mode="deerflow",
                deerflow_command=[sys.executable, "-c", script], auto_approve=False,
            ))
            evidence = ArtifactStore(root).get_artifact(task.artifacts[0])
        self.assertEqual(task.state, ResearchState.AWAITING_APPROVAL)
        self.assertEqual(evidence["payload"]["records"][0]["source"], "deerflow")

    def test_empty_evidence_stops_workflow_after_persisting_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            agents = [
                LiteratureAgent([FixtureLiteratureSource([])]), FakeHypothesisAgent(), FakeCodingAgent(),
                FakeComputeAgent(), FakeAnalysisAgent(), FakeReviewerAgent(),
            ]
            task = asyncio.run(ResearchWorkflow(store, {agent.name: agent for agent in agents}).run(ResearchTask("question")))
            evidence = store.get_artifact(task.artifacts[0])
        self.assertEqual(task.state, ResearchState.FAILED)
        self.assertEqual(evidence["status"], "insufficient_evidence")
        self.assertEqual(evidence["payload"]["source_snapshots"][0]["status"], "success")

    def test_workflow_rejects_wrong_artifact_kind_from_agent(self):
        class WrongCodingAgent:
            name = "coding"
            capabilities = ("implement_experiment",)

            async def handle(self, message, task):
                return Artifact("Finding", {"finding": "wrong stage"}, "coding")

        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            agents = [
                LiteratureAgent([FixtureLiteratureSource()]), FakeHypothesisAgent(),
                ClaimEvidenceAgent(), WrongCodingAgent(), FakeComputeAgent(),
                MetricsAnalysisAgent(), EvidenceReviewAgent(), ReportWriterAgent(),
            ]
            task = asyncio.run(ResearchWorkflow(
                store, {agent.name: agent for agent in agents},
            ).run(ResearchTask("wrong contract")))
            records = [store.get_artifact(item) for item in task.artifacts]
        self.assertEqual(task.state, ResearchState.FAILED)
        violation = records[-1]
        self.assertEqual(violation["kind"], "AgentContractViolation")
        self.assertEqual(violation["status"], "failed")
        self.assertEqual(violation["payload"]["expected_kind"], "CodeRevision")
        self.assertEqual(violation["payload"]["returned_kind"], "Finding")
        self.assertEqual(violation["inputs"][-1], records[-2]["artifact_id"])

    def test_workflow_preserves_remote_failure_before_contract_violation(self):
        class RemoteFailureCodingAgent:
            name = "coding"
            capabilities = ("implement_experiment",)

            async def handle(self, message, task):
                return Artifact(
                    "A2ATaskFailure", {"provider": "a2a", "error": "peer unavailable"},
                    "coding", status="failed",
                )

        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            agents = [
                LiteratureAgent([FixtureLiteratureSource()]), FakeHypothesisAgent(),
                ClaimEvidenceAgent(), RemoteFailureCodingAgent(), FakeComputeAgent(),
                MetricsAnalysisAgent(), EvidenceReviewAgent(), ReportWriterAgent(),
            ]
            task = asyncio.run(ResearchWorkflow(
                store, {agent.name: agent for agent in agents},
            ).run(ResearchTask("remote failure provenance")))
            records = [store.get_artifact(item) for item in task.artifacts]
        self.assertEqual(task.state, ResearchState.FAILED)
        remote_failure = next(record for record in records if record["kind"] == "A2ATaskFailure")
        violation = records[-1]
        self.assertEqual(remote_failure["payload"]["error"], "peer unavailable")
        self.assertEqual(violation["kind"], "AgentContractViolation")
        self.assertIn(remote_failure["artifact_id"], violation["inputs"])

    def test_retry_preserves_failed_attempt_artifacts_and_restarts_search_cleanly(self):
        class WrongCodingAgent:
            name = "coding"
            capabilities = ("implement_experiment",)

            async def handle(self, message, task):
                return Artifact("Finding", {"finding": "wrong stage"}, "coding")

        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            failing_agents = [
                LiteratureAgent([FixtureLiteratureSource()]), FakeHypothesisAgent(),
                ClaimEvidenceAgent(), WrongCodingAgent(), FakeComputeAgent(),
                MetricsAnalysisAgent(), EvidenceReviewAgent(), ReportWriterAgent(),
            ]
            task = asyncio.run(ResearchWorkflow(
                store, {agent.name: agent for agent in failing_agents},
            ).run(ResearchTask("retry provenance")))
            failed_artifact_ids = list(task.artifacts)
            task.reset_for_retry()
            store.put_task(task)
            succeeding_agents = [
                LiteratureAgent([FixtureLiteratureSource()]), FakeHypothesisAgent(),
                ClaimEvidenceAgent(), FakeCodingAgent(), FakeComputeAgent(),
                MetricsAnalysisAgent(), EvidenceReviewAgent(), ReportWriterAgent(),
            ]
            completed = asyncio.run(ResearchWorkflow(
                store, {agent.name: agent for agent in succeeding_agents},
            ).run(task))
            records = [store.get_artifact(item) for item in completed.artifacts]
            latest_evidence = [record for record in records if record["kind"] == "EvidenceSet"][-1]
        self.assertEqual(completed.state, ResearchState.REPORT_READY)
        self.assertTrue(set(failed_artifact_ids).issubset(completed.artifacts))
        self.assertTrue(any(record["kind"] == "AgentContractViolation" for record in records))
        self.assertEqual(latest_evidence["inputs"], [])

    def test_subprocess_coding_agent_uses_json_stdin_and_captures_output(self):
        with tempfile.TemporaryDirectory() as root:
            agent = SubprocessCodingAgent(["python", "-c", "import json,sys; r=json.load(sys.stdin); print(r['input_artifact_data'][0]['payload']['finding'])"], root)
            input_artifact = Artifact("Finding", {"finding": "previous result"}, "analysis")
            artifact = asyncio.run(agent.handle(A2AMessage(
                "task", "control", "command-coding", "implement_experiment",
                [input_artifact.artifact_id], [input_artifact.to_dict()],
            ), ResearchTask("question")))
        self.assertEqual(artifact.status, "created")
        self.assertEqual(artifact.payload["stdout"].strip(), "previous result")

    def test_subprocess_coding_agent_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            agent = SubprocessCodingAgent(["python", "-c", "raise SystemExit(3)"], root)
            artifact = asyncio.run(agent.handle(A2AMessage("task", "control", "command-coding", "implement_experiment"), ResearchTask("question")))
        self.assertEqual(artifact.status, "failed")
        self.assertEqual(artifact.payload["returncode"], 3)

    def test_coding_artifact_records_git_workspace_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(["git", "init", root], check=True, capture_output=True)
            command = [sys.executable, "-c", "from pathlib import Path; Path('generated.py').write_text('x = 1\\n'); print('changed')"]
            artifact = asyncio.run(SubprocessCodingAgent(command, root).handle(
                A2AMessage("task", "control", "coding", "implement_experiment"), ResearchTask("question")
            ))
            store = ArtifactStore(root)
            store.put_artifact(artifact)
            package = build_reproducibility_package(
                ResearchTask("question", artifacts=[artifact.artifact_id]), store,
            )
        self.assertEqual(artifact.status, "created")
        self.assertTrue(artifact.payload["workspace_before"]["is_git_repository"])
        self.assertTrue(artifact.payload["workspace_after"]["is_git_repository"])
        self.assertTrue(artifact.payload["workspace_change_detected"])
        self.assertIn("generated.py", artifact.payload["workspace_after"]["status"])
        self.assertEqual(package.payload["code_provenance"]["artifact_id"], artifact.artifact_id)
        self.assertTrue(package.payload["code_provenance"]["workspace_change_detected"])

    def test_subprocess_hypothesis_agent_validates_structured_response(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-c", "import json; print(json.dumps({'hypotheses':[{'id':'H1','statement':'An intervention improves score','metric':'score'}]}))"]
            agent = SubprocessHypothesisAgent(command, root)
            artifact = asyncio.run(agent.handle(A2AMessage("task", "control", "hypothesis", "generate_hypotheses"), ResearchTask("question")))
        self.assertEqual(artifact.status, "created")
        self.assertEqual(artifact.payload["hypotheses"][0]["id"], "H1")

    def test_subprocess_hypothesis_agent_rejects_malformed_response(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-c", "print('{\"hypotheses\":[{\"statement\":\"missing id\"}]}')"]
            artifact = asyncio.run(SubprocessHypothesisAgent(command, root).handle(
                A2AMessage("task", "control", "hypothesis", "generate_hypotheses"), ResearchTask("question")
            ))
        self.assertEqual(artifact.status, "failed")
        self.assertIn("validation_error", artifact.payload)

    def test_subprocess_analysis_agent_validates_and_exposes_normalized_result(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-c", "import json; print(json.dumps({'finding':'Observed improvement','confidence':'descriptive_only','metrics':{'score':0.8}}))"]
            artifact = asyncio.run(SubprocessAnalysisAgent(command, root).handle(
                A2AMessage("task", "control", "analysis", "analyze_results"), ResearchTask("question")
            ))
        self.assertEqual(artifact.status, "created")
        self.assertEqual(artifact.payload["finding"], "Observed improvement")
        self.assertEqual(artifact.payload["result"]["metrics"], {"score": 0.8})

    def test_subprocess_analysis_agent_rejects_malformed_response(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-c", "print('{\\\"finding\\\":\\\"x\\\",\\\"confidence\\\":\\\"invalid\\\"}')"]
            artifact = asyncio.run(SubprocessAnalysisAgent(command, root).handle(
                A2AMessage("task", "control", "analysis", "analyze_results"), ResearchTask("question")
            ))
        self.assertEqual(artifact.status, "failed")
        self.assertIn("validation_error", artifact.payload)

    def test_subprocess_reviewer_agent_validates_structured_response(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-c", "import json; print(json.dumps({'decision':'pass_with_human_review','blocking_issues':[]}))"]
            artifact = asyncio.run(SubprocessReviewerAgent(command, root).handle(
                A2AMessage("task", "control", "reviewer", "review"), ResearchTask("question")
            ))
        self.assertEqual(artifact.status, "created")
        self.assertEqual(artifact.payload["decision"], "pass_with_human_review")

    def test_subprocess_reviewer_agent_blocks_workflow_on_blocked_decision(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-c", "import json; print(json.dumps({'decision':'blocked','blocking_issues':['missing control']}))"]
            artifact = asyncio.run(SubprocessReviewerAgent(command, root).handle(
                A2AMessage("task", "control", "reviewer", "review"), ResearchTask("question")
            ))
        self.assertEqual(artifact.status, "failed")
        self.assertEqual(artifact.payload["decision"], "blocked")

    def test_workflow_can_use_external_hypothesis_agent(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-c", "import json; print(json.dumps({'hypotheses':[{'id':'HX','statement':'An intervention improves the target score','metric':'score'}]}))"]
            task = asyncio.run(run_research("external hypothesis", root, hypothesis_command=command))
            store = ArtifactStore(root)
            hypothesis = next(store.get_artifact(item) for item in task.artifacts if store.get_artifact(item)["kind"] == "HypothesisSet")
            report = store.get_artifact(task.artifacts[-1])
        self.assertEqual(task.state, ResearchState.REPORT_READY)
        self.assertEqual(hypothesis["payload"]["hypotheses"][0]["id"], "HX")
        self.assertEqual(report["payload"]["hypotheses"][0]["id"], "HX")

    def test_workflow_can_use_external_analysis_and_reviewer_agents(self):
        with tempfile.TemporaryDirectory() as root:
            analysis_command = [sys.executable, "-c", "import json; print(json.dumps({'finding':'External finding','confidence':'descriptive_only','metrics':{'score':0.7}}))"]
            reviewer_command = [sys.executable, "-c", "import json; print(json.dumps({'decision':'pass_with_human_review','blocking_issues':[]}))"]
            task = asyncio.run(run_research(
                "external analysis", root, analysis_command=analysis_command,
                reviewer_command=reviewer_command,
            ))
            store = ArtifactStore(root)
            finding = next(store.get_artifact(item) for item in task.artifacts if store.get_artifact(item)["kind"] == "Finding")
            review = next(store.get_artifact(item) for item in task.artifacts if store.get_artifact(item)["kind"] == "ReviewReport")
        self.assertEqual(task.state, ResearchState.REPORT_READY)
        self.assertEqual(finding["payload"]["finding"], "External finding")
        self.assertEqual(review["payload"]["decision"], "pass_with_human_review")

    def test_claude_code_adapter_preserves_structured_response(self):
        class FakeClaude(ClaudeCodeAgent):
            def _command(self, prompt):
                return [sys.executable, "-c", "import json; print(json.dumps({'result':'changed experiment'}))"]

        with tempfile.TemporaryDirectory() as root:
            agent = FakeClaude(root)
            artifact = asyncio.run(agent.handle(
                A2AMessage("task", "control", "coding", "implement_experiment", ["hypothesis-1"]),
                ResearchTask("question"),
            ))
        self.assertEqual(artifact.status, "created")
        self.assertEqual(artifact.payload["provider"], "claude-code")
        self.assertEqual(artifact.payload["response"]["result"], "changed experiment")
        self.assertIn("input_artifact_data", artifact.payload["request"])

    def test_extract_result_accepts_metrics_json(self):
        self.assertEqual(extract_result('{"metrics":{"accuracy":0.91}}'), ({"accuracy": 0.91}, "json"))
        self.assertEqual(extract_result("log\nAUTORESEARCH_RESULT: {\"loss\": 0.2}"), ({"loss": 0.2}, "json"))
        self.assertIsNone(extract_result("no metrics")[0])

    def test_extract_result_accepts_flat_report_with_metadata(self):
        output = '{"baseline_score":0.3333,"candidate_score":0.9333,"experiment":"iris_classification","score":0.9333,"n_test":30}'
        metrics, status = extract_result(output, "auto")
        self.assertEqual(status, "json")
        self.assertAlmostEqual(metrics["score"], 0.9333)
        self.assertAlmostEqual(metrics["candidate_score"], 0.9333)

    def test_extract_result_accepts_noisy_and_nested_json(self):
        output = 'training...\n```json\n{"evaluation":{"accuracy":0.97,"f1":0.96}}\n```\nfinished'
        metrics, status = extract_result(output)
        self.assertEqual(status, "json")
        self.assertEqual(metrics, {"accuracy": 0.97, "f1": 0.96})

    def test_extract_result_accepts_model_comparison_output(self):
        output = '{"models":{"knn":{"accuracy":0.986},"logistic":{"accuracy":0.958}}}'
        metrics, status = extract_result(output, "score")
        self.assertEqual(status, "json_models")
        self.assertAlmostEqual(metrics["accuracy"], 0.986)
        self.assertAlmostEqual(metrics["score"], 0.986)

    def test_parse_command_preserves_subcommand_flags(self):
        self.assertEqual(parse_command("python -c \"print('ok')\""), ["python", "-c", "print('ok')"])

    def test_local_compute_agent_records_metrics_and_environment(self):
        with tempfile.TemporaryDirectory() as root:
            agent = LocalComputeAgent(["python", "-c", "print('{\\\"metrics\\\":{\\\"score\\\":0.75}}')"], root)
            artifact = asyncio.run(agent.handle(A2AMessage("task", "control", "command-compute", "run_experiment"), ResearchTask("question")))
        self.assertEqual(artifact.status, "created")
        self.assertEqual(artifact.payload["metrics"], {"score": 0.75})
        self.assertIn("python", artifact.payload["environment"])

    def test_local_compute_agent_rejects_missing_metrics(self):
        with tempfile.TemporaryDirectory() as root:
            agent = LocalComputeAgent(["python", "-c", "print('finished')"], root)
            artifact = asyncio.run(agent.handle(A2AMessage("task", "control", "command-compute", "run_experiment"), ResearchTask("question")))
        self.assertEqual(artifact.status, "failed")
        self.assertEqual(artifact.payload["metrics_status"], "missing_or_invalid")

    def test_docker_compute_agent_builds_restricted_command(self):
        class RecordingDocker(DockerComputeAgent):
            async def _run(self, command, cwd):
                self.captured = list(command)
                return 0, '{"metrics":{"score":1}}', "", None

        with tempfile.TemporaryDirectory() as root:
            agent = RecordingDocker("python:3.12-slim", ["python", "experiment.py"], root)
            artifact = asyncio.run(agent.handle(A2AMessage("task", "control", "docker-compute", "run_experiment"), ResearchTask("question")))
        self.assertEqual(artifact.status, "created")
        self.assertIn("--network", agent.captured)
        self.assertIn("--read-only", agent.captured)
        self.assertIn("--cap-drop", agent.captured)
        self.assertIn("ALL", agent.captured)
        self.assertIn("none", agent.captured)
        self.assertTrue(any(item.endswith(":/workspace:ro") for item in agent.captured))

    def test_a2a_card_discovery_and_structured_artifact_round_trip(self):
        local = LiteratureAgent([FixtureLiteratureSource()])
        with A2AAgentServer(local) as server:
            remote = A2AHttpAgent("literature", server.base_url)
            card = asyncio.run(remote.discover())
            artifact = asyncio.run(remote.handle(
                A2AMessage("task-1", "control", "literature", "search_literature"), ResearchTask("question", task_id="task-1")
            ))
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "HTTP+JSON")
        self.assertEqual(artifact.kind, "EvidenceSet")
        self.assertEqual(artifact.payload["summary"]["record_count"], 1)

    def test_a2a_client_rejects_tampered_remote_artifact_hash(self):
        remote = A2AHttpAgent("analysis", "http://127.0.0.1:9999")
        artifact = Artifact("Finding", {"finding": "remote"}, "analysis")
        response = {
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"parts": [{"data": {**artifact.to_dict(), "content_hash": "tampered"}}]}],
        }
        card = {"supportedInterfaces": [{"protocolBinding": "HTTP+JSON"}]}
        with patch("autoresearch.a2a._read_json", side_effect=[card, response]):
            with self.assertRaises(A2AError):
                asyncio.run(remote.handle(
                    A2AMessage("task", "control", "analysis", "analyze_results"), ResearchTask("question")
                ))

    def test_a2a_client_preserves_remote_failure_as_failed_artifact(self):
        remote = A2AHttpAgent("analysis", "http://127.0.0.1:9999")
        card = {"supportedInterfaces": [{"protocolBinding": "HTTP+JSON"}]}
        response = {
            "status": {"state": "TASK_STATE_FAILED"},
            "metadata": {"error": "remote reviewer unavailable"},
        }
        with patch("autoresearch.a2a._read_json", side_effect=[card, response]):
            artifact = asyncio.run(remote.handle(
                A2AMessage("task", "control", "analysis", "analyze_results"), ResearchTask("question")
            ))
        self.assertEqual(artifact.kind, "A2ATaskFailure")
        self.assertEqual(artifact.status, "failed")
        self.assertEqual(artifact.payload["error"], "remote reviewer unavailable")

    def test_workflow_can_route_literature_to_a2a_agent(self):
        local = LiteratureAgent([FixtureLiteratureSource()])
        with tempfile.TemporaryDirectory() as root, A2AAgentServer(local) as server:
            task = asyncio.run(run_research("question", root, literature_a2a_url=server.base_url))
        self.assertEqual(task.state, ResearchState.REPORT_READY)

    def test_workflow_can_route_coding_stage_to_a2a_agent(self):
        remote_coding = FakeCodingAgent()
        with tempfile.TemporaryDirectory() as root, A2AAgentServer(remote_coding) as server:
            task = asyncio.run(run_research("remote coding", root, a2a_urls={"coding": server.base_url}))
            coding = next(
                ArtifactStore(root).get_artifact(artifact_id)
                for artifact_id in task.artifacts
                if ArtifactStore(root).get_artifact(artifact_id)["kind"] == "CodeRevision"
            )
        self.assertEqual(task.state, ResearchState.REPORT_READY)
        self.assertEqual(coding["producer"], "coding")
        self.assertEqual(coding["payload"]["commit"], "fake0001")

    def test_persisted_approval_can_be_resumed(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            # Create a fresh paused task through the workflow so the persisted state is authoritative.
            paused = ResearchTask("paused")
            paused = asyncio.run(run_research("paused", root, auto_approve=False))
            resumed = asyncio.run(resume_research(paused.task_id, root))
        self.assertEqual(resumed.state, ResearchState.REPORT_READY)
        self.assertEqual(len(resumed.artifacts), 9)

    def test_report_writer_emits_provenance_linked_report(self):
        artifacts = [
            Artifact("EvidenceSet", {"summary": {"record_count": 1, "candidate_passage_count": 2}}, "literature"),
            Artifact("HypothesisSet", {"hypotheses": [{"id": "H1"}]}, "hypothesis"),
            Artifact("ExperimentRun", {"metrics": {"score": 0.8}}, "compute"),
            Artifact("Finding", {"finding": "Observed improvement", "confidence": "descriptive_only"}, "analysis"),
            Artifact("ReviewReport", {"decision": "requires_human_review"}, "reviewer"),
            Artifact("ReproducibilityPackage", {"status": "validated"}, "control-plane"),
        ]
        report = asyncio.run(ReportWriterAgent().handle(
            A2AMessage("task", "control", "report", "write_report", [a.artifact_id for a in artifacts], [a.to_dict() for a in artifacts]),
            ResearchTask("question"),
        ))
        self.assertEqual(report.status, "created")
        self.assertEqual(report.kind, "ResearchReport")
        self.assertEqual(report.payload["report_status"], "draft_for_human_review")
        self.assertEqual(report.payload["experiment"]["artifact_id"], artifacts[2].artifact_id)
        self.assertEqual(report.payload["reproducibility_artifact_id"], artifacts[5].artifact_id)

    def test_report_writer_marks_legacy_empty_run_as_synthetic(self):
        artifacts = [
            Artifact("EvidenceSet", {"summary": {}}, "literature"),
            Artifact("HypothesisSet", {"hypotheses": []}, "hypothesis"),
            Artifact("ExperimentRun", {"metrics": {"score": 0.81}, "run_role": "candidate"}, "compute"),
            Artifact("Finding", {"finding": "Observed descriptive statistics"}, "analysis"),
            Artifact("ReviewReport", {"decision": "requires_human_review"}, "reviewer"),
            Artifact("ReproducibilityPackage", {"status": "validated"}, "control-plane"),
        ]
        report = asyncio.run(ReportWriterAgent().handle(
            A2AMessage("task", "control", "report", "write_report", [a.artifact_id for a in artifacts], [a.to_dict() for a in artifacts]),
            ResearchTask("legacy fake"),
        ))
        self.assertEqual(report.payload["execution_mode"], "synthetic_demo")

    def test_report_writer_marks_local_run_as_real(self):
        artifacts = [
            Artifact("EvidenceSet", {"summary": {}}, "literature"),
            Artifact("HypothesisSet", {"hypotheses": []}, "hypothesis"),
            Artifact("ExperimentRun", {"executor": "local", "command": ["python", "experiment.py"], "cwd": ".", "returncode": 0, "stdout": '{"accuracy": 0.9}', "metrics": {"accuracy": 0.9}, "run_role": "candidate"}, "compute"),
            Artifact("Finding", {"finding": "Observed descriptive statistics"}, "analysis"),
            Artifact("ReviewReport", {"decision": "requires_human_review"}, "reviewer"),
            Artifact("ReproducibilityPackage", {"status": "validated"}, "control-plane"),
        ]
        report = asyncio.run(ReportWriterAgent().handle(
            A2AMessage("task", "control", "report", "write_report", [a.artifact_id for a in artifacts], [a.to_dict() for a in artifacts]),
            ResearchTask("local real"),
        ))
        self.assertEqual(report.payload["execution_mode"], "real_configured")

    def test_report_writer_ignores_older_fake_run_after_real_retry(self):
        artifacts = [
            Artifact("EvidenceSet", {"summary": {}}, "literature"),
            Artifact("HypothesisSet", {"hypotheses": []}, "hypothesis"),
            Artifact("ExperimentRun", {"executor": "fake", "metrics": {"score": 0.81}, "run_role": "candidate"}, "compute"),
            Artifact("ExperimentRun", {"executor": "local", "command": ["python", "experiment.py"], "cwd": ".", "returncode": 0, "stdout": '{"score": 0.97}', "metrics": {"score": 0.97}, "run_role": "candidate"}, "compute"),
            Artifact("Finding", {"finding": "Observed descriptive statistics"}, "analysis"),
            Artifact("ReviewReport", {"decision": "requires_human_review"}, "reviewer"),
            Artifact("ReproducibilityPackage", {"status": "validated"}, "control-plane"),
        ]
        report = asyncio.run(ReportWriterAgent().handle(
            A2AMessage("task", "control", "report", "write_report", [a.artifact_id for a in artifacts], [a.to_dict() for a in artifacts]),
            ResearchTask("retry real"),
        ))
        self.assertEqual(report.payload["execution_mode"], "real_configured")

    def test_http_control_plane_creates_paused_task_and_approves_it(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            request = Request(
                server.base_url + "/research", method="POST",
                data=json.dumps({"question": "api question"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 201)
                created = json.loads(response.read())
            task = created["task"]
            self.assertEqual(task["state"], ResearchState.AWAITING_APPROVAL.value)

            approve = Request(
                server.base_url + f"/research/{task['task_id']}/approve", method="POST",
                data=b"{}", headers={"Content-Type": "application/json"},
            )
            with urlopen(approve) as response:
                resumed = json.loads(response.read())["task"]
            self.assertEqual(resumed["state"], ResearchState.REPORT_READY.value)

    def test_http_control_plane_lists_task_summaries(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            with urlopen(server.base_url + "/research") as response:
                self.assertEqual(json.loads(response.read())["tasks"], [])
            first = ResearchTask("first task")
            first.transition(ResearchState.SEARCHING)
            second = ResearchTask("second task")
            second.transition(ResearchState.SEARCHING)
            store = ArtifactStore(root)
            store.put_task(first)
            store.put_task(second)
            with urlopen(server.base_url + "/research") as response:
                tasks = json.loads(response.read())["tasks"]
        self.assertEqual({task["task_id"] for task in tasks}, {first.task_id, second.task_id})
        self.assertEqual({task["question"] for task in tasks}, {"first task", "second task"})
        self.assertTrue(all("artifact_count" in task for task in tasks))

    def test_http_control_plane_filters_task_summaries(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            paused = ResearchTask("paused", state=ResearchState.AWAITING_APPROVAL)
            paused.execution_status = "not_queued"
            completed = ResearchTask("completed", state=ResearchState.REPORT_READY)
            completed.execution_status = "succeeded"
            store = ArtifactStore(root)
            store.put_task(paused)
            store.put_task(completed)
            with urlopen(server.base_url + "/research?state=AWAITING_APPROVAL") as response:
                by_state = json.loads(response.read())["tasks"]
            with urlopen(server.base_url + "/research?execution_status=succeeded") as response:
                by_execution = json.loads(response.read())["tasks"]
            with self.assertRaises(HTTPError) as invalid_state:
                urlopen(server.base_url + "/research?state=unknown")
            with self.assertRaises(HTTPError) as unknown_filter:
                urlopen(server.base_url + "/research?owner=test")
        self.assertEqual([task["task_id"] for task in by_state], [paused.task_id])
        self.assertEqual([task["task_id"] for task in by_execution], [completed.task_id])
        self.assertEqual(invalid_state.exception.code, 400)
        self.assertEqual(unknown_filter.exception.code, 400)

    def test_http_resume_keeps_cancelled_task_terminal(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            task = ResearchTask("cancelled api", state=ResearchState.CANCELLED, error="cancel requested")
            ArtifactStore(root).put_task(task)
            request = Request(
                server.base_url + f"/research/{task.task_id}/resume", method="POST",
                data=b"{}", headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                result = json.loads(response.read())["task"]
        self.assertEqual(result["state"], ResearchState.CANCELLED.value)
        self.assertEqual(result["error"], "cancel requested")
        self.assertEqual(result["artifacts"], [])

    def test_http_retry_resubmits_orphaned_task_with_explicit_profile(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            store = ArtifactStore(root)
            task = ResearchTask("orphan retry")
            store.put_task(task)
            store.put_job("orphan-job", {
                "job_id": "orphan-job", "task_id": task.task_id,
                "status": "running", "created_at": "2026-01-01T00:00:00+00:00",
            })
            # Restarting the control plane is represented by a second server;
            # its constructor marks the stale job orphaned.
            server.close()
            with ResearchApiServer(root) as restarted:
                retry = Request(
                    restarted.base_url + f"/research/{task.task_id}/retry", method="POST",
                    data=json.dumps({"max_attempts": 1}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(retry) as response:
                    queued = json.loads(response.read())
                job_id = queued["job"]["job_id"]
                final_job = None
                for _ in range(100):
                    with urlopen(restarted.base_url + f"/research/{task.task_id}/job") as response:
                        final_job = json.loads(response.read())["job"]
                    if final_job["status"] in {"succeeded", "failed", "cancelled"}:
                        break
                    time.sleep(0.02)
                with urlopen(restarted.base_url + f"/research/{task.task_id}") as response:
                    final_task = json.loads(response.read())["task"]
        self.assertEqual(queued["task"]["execution_status"], "queued")
        self.assertEqual(final_job["job_id"], job_id)
        self.assertEqual(final_job["status"], "succeeded")
        self.assertEqual(final_task["state"], ResearchState.REPORT_READY.value)

    def test_http_control_plane_lists_and_reads_task_artifacts(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            request = Request(
                server.base_url + "/research", method="POST",
                data=json.dumps({"question": "artifact api", "auto_approve": True}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                task = json.loads(response.read())["task"]
            with urlopen(server.base_url + f"/research/{task['task_id']}/artifacts") as response:
                listed = json.loads(response.read())
            artifact_id = task["artifacts"][0]
            with urlopen(server.base_url + f"/research/{task['task_id']}/artifacts/{artifact_id}") as response:
                single = json.loads(response.read())
            missing = Request(
                server.base_url + f"/research/{task['task_id']}/artifacts/not-a-task-artifact",
                method="GET",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(missing)
        self.assertEqual([item["artifact_id"] for item in listed["artifacts"]], task["artifacts"])
        self.assertEqual(single["artifact"]["artifact_id"], artifact_id)
        self.assertEqual(getattr(raised.exception, "code", None), 404)

    def test_http_control_plane_returns_consolidated_research_status(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            request = Request(
                server.base_url + "/research", method="POST",
                data=json.dumps({"question": "status api", "auto_approve": True}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                task = json.loads(response.read())["task"]
            with urlopen(server.base_url + f"/research/{task['task_id']}/status") as response:
                status = json.loads(response.read())["status"]
            with self.assertRaises(HTTPError) as missing:
                urlopen(server.base_url + "/research/missing-task/status")
        self.assertEqual(status["task_id"], task["task_id"])
        self.assertEqual(status["state"], ResearchState.REPORT_READY.value)
        self.assertEqual(status["evidence"]["summary"]["record_count"], 1)
        self.assertEqual(status["finding"]["confidence"], "descriptive_only")
        self.assertEqual(status["review"]["decision"], "requires_human_review")
        self.assertEqual(status["report"]["status"], "draft_for_human_review")
        self.assertIsNotNone(status["reproducibility_artifact_id"])
        self.assertEqual(missing.exception.code, 404)

    def test_http_control_plane_accepts_external_hypothesis_command(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            command = [sys.executable, "-c", "import json; print(json.dumps({'hypotheses':[{'id':'API-H1','statement':'An intervention improves score'}]}))"]
            request = Request(
                server.base_url + "/research", method="POST",
                data=json.dumps({"question": "api hypothesis", "auto_approve": True, "hypothesis_command": command, "hypothesis_cwd": root}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                payload = json.loads(response.read())
            task = payload["task"]
            store = ArtifactStore(root)
            hypothesis = next(store.get_artifact(item) for item in task["artifacts"] if store.get_artifact(item)["kind"] == "HypothesisSet")
        self.assertEqual(task["state"], ResearchState.REPORT_READY.value)
        self.assertEqual(hypothesis["payload"]["hypotheses"][0]["id"], "API-H1")

    def test_http_control_plane_accepts_external_analysis_and_reviewer_commands(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            analysis_command = [sys.executable, "-c", "import json; print(json.dumps({'finding':'API finding','confidence':'descriptive_only'}))"]
            reviewer_command = [sys.executable, "-c", "import json; print(json.dumps({'decision':'pass_with_human_review'}))"]
            request = Request(
                server.base_url + "/research", method="POST",
                data=json.dumps({
                    "question": "api analysis",
                    "auto_approve": True,
                    "analysis_command": analysis_command,
                    "analysis_cwd": root,
                    "reviewer_command": reviewer_command,
                    "reviewer_cwd": root,
                }).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                payload = json.loads(response.read())
            task = payload["task"]
            store = ArtifactStore(root)
            finding = next(store.get_artifact(item) for item in task["artifacts"] if store.get_artifact(item)["kind"] == "Finding")
            review = next(store.get_artifact(item) for item in task["artifacts"] if store.get_artifact(item)["kind"] == "ReviewReport")
        self.assertEqual(task["state"], ResearchState.REPORT_READY.value)
        self.assertEqual(finding["payload"]["finding"], "API finding")
        self.assertEqual(review["payload"]["decision"], "pass_with_human_review")

    def test_http_control_plane_records_evidence_adjudication_and_new_report(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            request = Request(
                server.base_url + "/research", method="POST",
                data=json.dumps({"question": "adjudication api", "auto_approve": True}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                created = json.loads(response.read())["task"]
            store = ArtifactStore(root)
            claim_map = next(store.get_artifact(item) for item in created["artifacts"] if store.get_artifact(item)["kind"] == "ClaimEvidenceMap")
            claim = claim_map["payload"]["claims"][0]
            passage_id = claim["candidates"][0]["passage_id"] if claim["candidates"] else None
            decision = {"claim_id": claim["claim_id"], "decision": "uncertain", "note": "Requires direct source review."}
            if passage_id:
                decision["passage_id"] = passage_id
            adjudicate = Request(
                server.base_url + f"/research/{created['task_id']}/adjudicate", method="POST",
                data=json.dumps({"adjudicator": "researcher", "decisions": [decision]}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(adjudicate) as response:
                result = json.loads(response.read())
            final_task = result["task"]
            report = result["report"]
        self.assertEqual(final_task["state"], ResearchState.REPORT_READY.value)
        self.assertEqual(result["adjudication"]["kind"], "EvidenceAdjudication")
        self.assertEqual(report["payload"]["evidence_adjudication"]["summary"]["uncertain_count"], 1)
        self.assertEqual(report["payload"]["report_status"], "draft_for_human_review")

    def test_http_control_plane_runs_async_job_and_persists_report(self):
        with tempfile.TemporaryDirectory() as root, ResearchApiServer(root) as server:
            request = Request(
                server.base_url + "/research", method="POST",
                data=json.dumps({"question": "async question", "async": True, "auto_approve": True}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 202)
                created = json.loads(response.read())
            task_id = created["task"]["task_id"]
            self.assertIn(created["job"]["status"], {"queued", "running", "succeeded"})
            final_job = None
            for _ in range(100):
                with urlopen(server.base_url + f"/research/{task_id}/job") as response:
                    final_job = json.loads(response.read())["job"]
                if final_job["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.02)
            self.assertEqual(final_job["status"], "succeeded")
            with urlopen(server.base_url + f"/research/{task_id}") as response:
                final_task = json.loads(response.read())["task"]
            self.assertEqual(final_task["execution_status"], "succeeded")
            self.assertEqual(final_task["state"], ResearchState.REPORT_READY.value)

    def test_background_runner_retries_failed_task_with_same_id(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            task = ResearchTask("retry")
            store.put_task(task)
            calls = []

            def execute():
                calls.append(1)
                current = store.get_task(task.task_id)
                if len(calls) == 1:
                    current.transition(ResearchState.FAILED)
                else:
                    current.state = ResearchState.REPORT_READY
                store.put_task(current)
                return current

            with BackgroundTaskRunner(store) as runner:
                job = runner.submit(task.task_id, execute, max_attempts=2)
                for _ in range(100):
                    current_job = store.get_job(job.job_id)
                    if current_job["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.02)
                self.assertEqual(current_job["status"], "succeeded")
                current_task = store.get_task(task.task_id)
            self.assertEqual(len(calls), 2)
            self.assertEqual(current_task.state, ResearchState.REPORT_READY)
            self.assertEqual(current_task.attempt, 2)

    def test_background_runner_marks_callback_profile_failure_as_failed(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            task = ResearchTask("callback failure", state=ResearchState.IMPLEMENTING)
            store.put_task(task)

            def execute():
                current = store.get_task(task.task_id)
                current.error = "execution profile does not match"
                return current

            with BackgroundTaskRunner(store) as runner:
                job = runner.submit(task.task_id, execute)
                for _ in range(100):
                    final_job = store.get_job(job.job_id)
                    if final_job["status"] == "failed":
                        break
                    time.sleep(0.02)
            current = store.get_task(task.task_id)
            self.assertEqual(final_job["status"], "failed")
            self.assertEqual(current.state, ResearchState.FAILED)
            self.assertEqual(current.execution_status, "failed")

    def test_background_runner_recovers_orphaned_job_and_lock(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            task = ResearchTask("orphan")
            store.put_task(task)
            job_id = "orphan-job"
            store.put_job(job_id, {"job_id": job_id, "task_id": task.task_id, "status": "running", "created_at": "2026-01-01T00:00:00+00:00"})
            (store.root / "locks" / f"{task.task_id}.lock").write_text("stale", encoding="ascii")
            with BackgroundTaskRunner(store) as runner:
                recovered = runner.recover_orphaned()
            self.assertEqual(recovered[0]["status"], "orphaned")
            self.assertFalse((store.root / "locks" / f"{task.task_id}.lock").exists())
            self.assertEqual(store.get_task(task.task_id).execution_status, "orphaned")

    def test_background_runner_does_not_recover_job_with_live_lock_owner(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            task = ResearchTask("live owner")
            store.put_task(task)
            job_id = "live-job"
            store.put_job(job_id, {"job_id": job_id, "task_id": task.task_id, "status": "running", "created_at": "2026-01-01T00:00:00+00:00"})
            (store.root / "locks" / f"{task.task_id}.lock").write_text(f"pid={os.getpid()}\n", encoding="ascii")
            with BackgroundTaskRunner(store) as runner:
                self.assertEqual(runner.recover_orphaned(), [])
            self.assertEqual(store.get_job(job_id)["status"], "running")
            self.assertTrue((store.root / "locks" / f"{task.task_id}.lock").exists())

    def test_background_runner_cancellation_is_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            task = ResearchTask("cancel")
            store.put_task(task)
            started = threading.Event()
            release = threading.Event()

            def execute():
                started.set()
                release.wait(2)
                current = store.get_task(task.task_id)
                current.state = ResearchState.REPORT_READY
                store.put_task(current)
                return current

            with BackgroundTaskRunner(store) as runner:
                job = runner.submit(task.task_id, execute)
                self.assertTrue(started.wait(1))
                runner.cancel(task.task_id)
                release.set()
                for _ in range(100):
                    final_job = store.get_job(job.job_id)
                    if final_job["status"] in {"succeeded", "failed", "cancelled"}:
                        break
                    time.sleep(0.02)
            self.assertEqual(final_job["status"], "cancelled")
            self.assertEqual(store.get_task(task.task_id).execution_status, "cancelled")

    def test_workflow_stops_at_agent_boundary_after_durable_cancellation(self):
        class CancellingCodingAgent:
            name = "coding"
            capabilities = ("implement_experiment",)

            async def handle(self, message, task):
                persisted = store.get_task(task.task_id)
                persisted.cancel_requested = True
                store.put_task(persisted)
                return Artifact("CodeRevision", {"commit": "cancelled-after-code"}, "coding")

        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            agents = [
                LiteratureAgent([FixtureLiteratureSource()]), FakeHypothesisAgent(),
                ClaimEvidenceAgent(), CancellingCodingAgent(), FakeComputeAgent(),
                MetricsAnalysisAgent(), EvidenceReviewAgent(), ReportWriterAgent(),
            ]
            task = asyncio.run(ResearchWorkflow(
                store, {agent.name: agent for agent in agents},
            ).run(ResearchTask("cancellation boundary")))
            records = [store.get_artifact(item) for item in task.artifacts]
        self.assertEqual(task.state, ResearchState.CANCELLED)
        self.assertEqual(task.error, "cancel requested")
        self.assertTrue(any(record["kind"] == "CodeRevision" for record in records))
        self.assertFalse(any(record["kind"] == "ExperimentRun" for record in records))
        self.assertFalse(any(record["kind"] == "ResearchReport" for record in records))

    def test_cancelled_task_is_an_idempotent_workflow_terminal_state(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            task = ResearchTask("already cancelled", state=ResearchState.CANCELLED, error="cancel requested")
            store.put_task(task)
            agents = [
                LiteratureAgent([FixtureLiteratureSource()]), FakeHypothesisAgent(),
                ClaimEvidenceAgent(), FakeCodingAgent(), FakeComputeAgent(),
                MetricsAnalysisAgent(), EvidenceReviewAgent(), ReportWriterAgent(),
            ]
            result = asyncio.run(ResearchWorkflow(
                store, {agent.name: agent for agent in agents},
            ).run(task))
        self.assertEqual(result.state, ResearchState.CANCELLED)
        self.assertEqual(result.error, "cancel requested")
        self.assertEqual(result.artifacts, [])

    def test_background_runner_does_not_mark_stale_success_after_cancellation(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            task = ResearchTask("cancel stale")
            store.put_task(task)
            started = threading.Event()
            release = threading.Event()

            def execute():
                started.set()
                release.wait(2)
                # Deliberately return an object created before cancellation.
                return ResearchTask("cancel stale", task_id=task.task_id, state=ResearchState.REPORT_READY)

            with BackgroundTaskRunner(store) as runner:
                job = runner.submit(task.task_id, execute)
                self.assertTrue(started.wait(1))
                runner.cancel(task.task_id)
                release.set()
                for _ in range(100):
                    final_job = store.get_job(job.job_id)
                    if final_job["status"] in {"succeeded", "failed", "cancelled"}:
                        break
                    time.sleep(0.02)
            self.assertEqual(final_job["status"], "cancelled")
            self.assertEqual(store.get_task(task.task_id).execution_status, "cancelled")

    def test_resume_rejects_changed_execution_profile(self):
        with tempfile.TemporaryDirectory() as root:
            paused = asyncio.run(run_research("profile", root, auto_approve=False))
            with self.assertRaises(ValueError):
                asyncio.run(resume_research(paused.task_id, root, literature_mode="live"))

    def test_metrics_analysis_is_descriptive_and_reads_artifact_context(self):
        run = Artifact("ExperimentRun", {"metrics": {"score": 0.8}}, "compute")
        message = A2AMessage("task", "control", "analysis", "analyze_results", [run.artifact_id], [run.to_dict()])
        finding = asyncio.run(MetricsAnalysisAgent().handle(message, ResearchTask("question")))
        self.assertEqual(finding.status, "created")
        self.assertEqual(finding.payload["metrics"], {"score": 0.8})
        self.assertEqual(finding.payload["confidence"], "descriptive_only")

    def test_metrics_analysis_uses_replicate_means_for_baseline_delta(self):
        baseline = Artifact("ExperimentRun", {"run_role": "baseline", "metrics": {"score": 0.4}}, "compute")
        replicates = [
            Artifact("ExperimentRun", {"run_role": "candidate", "replicate": 1, "metrics": {"score": 0.6}}, "compute"),
            Artifact("ExperimentRun", {"run_role": "candidate", "replicate": 2, "metrics": {"score": 0.8}}, "compute"),
            Artifact("ExperimentRun", {"run_role": "candidate", "replicate": 3, "metrics": {"score": 1.0}}, "compute"),
        ]
        artifacts = [baseline, *replicates]
        finding = asyncio.run(MetricsAnalysisAgent().handle(
            A2AMessage("task", "control", "analysis", "analyze_results", [item.artifact_id for item in artifacts], [item.to_dict() for item in artifacts]),
            ResearchTask("question", replicates=3),
        ))
        score = finding.payload["statistics"]["score"]
        self.assertEqual(score["n"], 3)
        self.assertAlmostEqual(score["mean"], 0.8)
        self.assertAlmostEqual(score["sample_stddev"], 0.2)
        self.assertAlmostEqual(score["standard_error"], 0.2 / (3 ** 0.5))
        self.assertAlmostEqual(finding.payload["metrics"]["score"], 0.8)
        self.assertAlmostEqual(finding.payload["delta_vs_baseline"]["score"], 0.4)
        self.assertEqual(finding.payload["replicate_artifact_ids"], [item.artifact_id for item in replicates])

    def test_workflow_reviews_and_reports_all_replicates(self):
        with tempfile.TemporaryDirectory() as root:
            task = asyncio.run(run_research("replicate trajectory", root, iterations=2, replicates=3))
            store = ArtifactStore(root)
            records = [store.get_artifact(artifact_id) for artifact_id in task.artifacts]
            runs = [record for record in records if record["kind"] == "ExperimentRun"]
            review = next(record for record in records if record["kind"] == "ReviewReport")
            package = next(record for record in records if record["kind"] == "ReproducibilityPackage")
            report = records[-1]
        self.assertEqual(len(runs), 6)
        self.assertEqual([run["payload"]["replicate"] for run in runs], [1, 2, 3, 1, 2, 3])
        self.assertIn("candidate_replicates=3", review["payload"]["reproducibility"])
        self.assertEqual(package["payload"]["experiment_statistics"]["score"]["n"], 3)
        self.assertEqual(report["payload"]["experiment"]["run_count"], 6)
        self.assertEqual(report["payload"]["experiment"]["statistics"]["score"]["n"], 3)

    def test_evidence_review_blocks_missing_experiment(self):
        review = asyncio.run(EvidenceReviewAgent().handle(
            A2AMessage("task", "control", "review", "review"), ResearchTask("question")
        ))
        self.assertEqual(review.status, "failed")
        self.assertEqual(review.payload["decision"], "blocked")

    def test_reproducibility_package_checks_hashes_and_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            first = store.put_artifact(Artifact("EvidenceSet", {"records": []}, "test"))
            task = ResearchTask("question", artifacts=[first.artifact_id])
            package = build_reproducibility_package(task, store)
        self.assertEqual(package.status, "validated")
        self.assertEqual(package.payload["artifact_manifest"][0]["artifact_id"], first.artifact_id)

    def test_artifact_store_rejects_conflicting_artifact_id_without_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            original = Artifact("EvidenceSet", {"records": ["original"]}, "test", artifact_id="fixed-id")
            store.put_artifact(original)
            # Repeating the exact immutable record is safe for retry handling.
            store.put_artifact(original)
            conflicting = Artifact("EvidenceSet", {"records": ["replacement"]}, "test", artifact_id="fixed-id")
            with self.assertRaises(FileExistsError):
                store.put_artifact(conflicting)
            persisted = store.get_artifact("fixed-id")
        self.assertEqual(persisted["payload"]["records"], ["original"])

    def test_reproducibility_package_uses_latest_attempt_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(root)
            first_evidence = store.put_artifact(Artifact("EvidenceSet", {"summary": {"record_count": 1}}, "literature"))
            first_review = store.put_artifact(Artifact("ReviewReport", {"decision": "blocked"}, "reviewer", status="failed"))
            latest_evidence = store.put_artifact(Artifact("EvidenceSet", {"summary": {"record_count": 2}}, "literature"))
            latest_review = store.put_artifact(Artifact("ReviewReport", {"decision": "requires_human_review"}, "reviewer"))
            task = ResearchTask("retry package", artifacts=[
                first_evidence.artifact_id, first_review.artifact_id,
                latest_evidence.artifact_id, latest_review.artifact_id,
            ])
            package = build_reproducibility_package(task, store)
        self.assertEqual(package.payload["evidence_summary"]["record_count"], 2)
        self.assertEqual(package.payload["review_artifact_id"], latest_review.artifact_id)
        self.assertEqual(len(package.payload["artifact_manifest"]), 4)
