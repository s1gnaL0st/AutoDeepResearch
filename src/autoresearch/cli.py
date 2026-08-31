from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shlex
from pathlib import Path

from .agents import ClaimEvidenceAgent, EvidenceReviewAgent, FakeCodingAgent, FakeComputeAgent, FakeHypothesisAgent, LiteratureAgent, MetricsAnalysisAgent, ReportWriterAgent, ResearchCriticAgent
from .coding import ClaudeCodeAgent, CodexCodingAgent, SubprocessCodingAgent
from .hypothesis import SubprocessHypothesisAgent
from .external_agents import SubprocessAnalysisAgent, SubprocessReviewerAgent
from .compute import AutoComputeAgent, DockerComputeAgent, LocalComputeAgent
from .a2a import A2AHttpAgent
from .literature import ArxivSource, CrossrefSource, DeepResearchSource, DeerFlowSource, FixtureLiteratureSource
from .models import ResearchTask
from .storage import create_store
from .workflow import ResearchWorkflow


def _discover_existing_experiment(command: list[str] | None, compute_cwd: str | None, coding_cwd: str | None) -> tuple[list[str] | None, str | None]:
    """Use a conventional existing experiment file when no command is given.

    This is deliberately conservative: only files in the explicitly supplied
    coding/compute workspace and a short allow-list are considered. Generated
    code still needs an explicit command unless it already exists before task
    creation, so an arbitrary repository script is never executed by surprise.
    """
    if command:
        return command, compute_cwd
    root = Path(compute_cwd or coding_cwd or ".").resolve()
    if not root.is_dir():
        return None, compute_cwd
    for filename in ("experiment.py", "candidate.py", "run_experiment.py"):
        if (root / filename).is_file():
            return ["python", filename], str(root)
    return None, compute_cwd


def _write_human_report(task: ResearchTask, root: str) -> str | None:
    """Write latest ResearchReport as readable Markdown alongside JSON artifacts."""
    store = create_store(root)
    for artifact_id in reversed(task.artifacts):
        record = store.get_artifact(artifact_id)
        if record.get("kind") != "ResearchReport":
            continue
        p = record.get("payload", {})
        exp, stats = p.get("experiment", {}), p.get("experiment", {}).get("statistics", {})
        lines = [f"# {p.get('title', 'AutoResearch report')}", "", f"**问题：** {p.get('question', '')}", "", p.get("executive_summary", ""), "", "## 实验结果", "",
                 f"- 指标：`{p.get('objective', {}).get('metric', '')}`（{p.get('objective', {}).get('direction', '')}）",
                 f"- 最佳值：`{p.get('objective', {}).get('best_value')}`", f"- 候选运行次数：{exp.get('run_count', 0)}", ""]
        for metric, value in exp.get("metrics", {}).items():
            s = stats.get(metric, {})
            lines.append(f"- **{metric}**：{value}（均值 {s.get('mean', value)}，n={s.get('n', exp.get('run_count', 0))}）")
        lines += ["", "## 结论（描述性）", "", p.get("finding", {}).get("text", ""), "", "## 审查状态", "",
                  f"- {p.get('review', {}).get('decision', 'requires_human_review')}", "- 本报告仍需人工科研审查。", "", "## 可复现信息", "",
                  f"- Reproducibility Artifact：`{p.get('reproducibility_artifact_id')}`", "- 原始 JSON 保存在同目录 `artifacts/`。"]
        feedback = p.get("feedback_loop", {}).get("decision", {})
        if feedback and feedback.get("status") != "not_run":
            lines[lines.index("## 审查状态"):lines.index("## 审查状态")] = ["## 反馈循环", "", f"- 决策：`{feedback.get('decision')}`", f"- 假设动作：`{feedback.get('hypothesis_action')}`", ""]
        path = Path(root) / "report.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manuscript = [
            f"# {p.get('title', 'Research manuscript draft')}", "",
            "> Status: `draft_for_human_review`. This outline is evidence-bound and contains placeholders where source or design facts are missing.", "",
            "## Abstract", "", p.get("executive_summary", "[AUTHOR_INPUT_NEEDED: abstract claim and contribution]") , "",
            "## Introduction", "", "[AUTHOR_INPUT_NEEDED: motivation, problem gap, and scope supported by verified citations.]", "",
            "## Related Work", "", "See `paper_cards.md` and `comparison_matrix.md`. Claims require source-grounded citation review.", "",
            "## Methods", "", "[AUTHOR_INPUT_NEEDED: data, preprocessing, model, implementation and reproducibility details.]", "",
            "## Research Plan", "", "The following plan fields were supplied to the hypothesis and coding stages:", "",
            f"- Baseline: `{p.get('research_plan', {}).get('baseline', 'AUTHOR_INPUT_NEEDED')}`",
            f"- Candidate: `{p.get('research_plan', {}).get('candidate', 'AUTHOR_INPUT_NEEDED')}`",
            f"- Metric: `{p.get('research_plan', {}).get('metric', p.get('objective', {}).get('metric', 'AUTHOR_INPUT_NEEDED'))}`",
            f"- Failure condition: `{p.get('research_plan', {}).get('failure_condition', 'AUTHOR_INPUT_NEEDED')}`",
            f"- Resource budget: `{p.get('research_plan', {}).get('resource_budget', 'AUTHOR_INPUT_NEEDED')}`", "",
            "## Experiments", "", f"The recorded objective is `{p.get('objective', {}).get('metric')}` with direction `{p.get('objective', {}).get('direction')}`.", "",
            "## Results", "", f"Observed candidate result: `{exp.get('metrics', {})}`. These are descriptive results from recorded runs.", "",
            "## Discussion", "", "[AUTHOR_INPUT_NEEDED: interpretation bounded by the evidence and comparison design.]", "",
            "## Limitations", "", "- Automated evidence mapping is not semantic entailment.\n- Novelty and causal validity require human review.\n- Inferential statistical tests are not supplied unless explicitly configured.", "",
            "## References", "", "[AUTHOR_INPUT_NEEDED: validate complete bibliographic metadata before submission.]", "",
            "## Nature-style QA", "", "- Quantitative claims must map to immutable ExperimentRun/Finding Artifacts.\n- Missing facts remain AUTHOR_INPUT_NEEDED.\n- Final status remains draft_for_human_review.", "",
        ]
        (Path(root) / "manuscript.md").write_text("\n".join(manuscript), encoding="utf-8")
        intelligence = next((store.get_artifact(aid) for aid in reversed(task.artifacts) if store.get_artifact(aid).get("kind") == "LiteratureIntelligence"), None)
        plan_artifact = next((store.get_artifact(aid) for aid in reversed(task.artifacts) if store.get_artifact(aid).get("kind") == "ResearchPlan"), None)
        if plan_artifact:
            plans = plan_artifact.get("payload", {}).get("plans", [])
            if plans and isinstance(plans[0], dict):
                # Keep the machine-readable report and the human manuscript
                # synchronized with the exact planner fields consumed above.
                p["research_plan"] = plans[0]
                manuscript[manuscript.index("## Research Plan") + 2: manuscript.index("## Experiments")] = [
                    "The following plan fields were supplied to the hypothesis and coding stages:", "",
                    *[f"- {key.replace('_', ' ').title()}: `{value}`" for key, value in plans[0].items()], "",
                ]
                (Path(root) / "manuscript.md").write_text("\n".join(manuscript), encoding="utf-8")
        if intelligence:
            ip = intelligence.get("payload", {})
            cards = ["# Paper Cards", ""]
            for card in ip.get("paper_cards", []):
                cards += [f"## {card.get('title', 'Untitled')}", "", f"- 证据级别：`{card.get('source_level', 'metadata_only')}`", f"- 方法：{card.get('method') or '未提供'}", f"- 相关性：{card.get('relevance') or '未提供'}", f"- 局限性：{'; '.join(card.get('limitations') or []) or '未提供'}", ""]
            (Path(root) / "paper_cards.md").write_text("\n".join(cards), encoding="utf-8")
            matrix = ["# Literature Comparison Matrix", ""]
            for row in ip.get("comparison_matrix", []):
                matrix.append(f"- **{row.get('dimension', 'dimension')}**：{row.get('comparison', '')}（{', '.join(row.get('paper_titles') or [])}）")
            (Path(root) / "comparison_matrix.md").write_text("\n".join(matrix) + "\n", encoding="utf-8")
            brief = ["# Innovation Brief", "", "> 以下均为候选创新点，不是已证明的新颖性结论。", ""]
            for gap in ip.get("gap_candidates", []):
                brief += [f"## {gap.get('statement', '')}", "", f"- 相关论文：{', '.join(gap.get('related_paper_titles') or []) or '未提供'}", f"- 反证搜索：{gap.get('falsification_search', '未提供')}", f"- 证据等级：`{gap.get('evidence_level', 'candidate_only')}`", ""]
            (Path(root) / "innovation_brief.md").write_text("\n".join(brief), encoding="utf-8")
            plans = ["# Research Plan", ""]
            for item in ip.get("research_plans", []):
                for key, value in item.items():
                    plans.append(f"- **{key}**：{value}")
            (Path(root) / "research_plan.md").write_text("\n".join(plans) + "\n", encoding="utf-8")
        return str(path)
    return None


def parse_command(value: str) -> list[str]:
    command = shlex.split(value)
    if not command:
        raise argparse.ArgumentTypeError("command must not be empty")
    return command


def build_execution_profile(
    literature_mode: str = "fixture",
    deepresearch_command: list[str] | None = None,
    deepresearch_cwd: str | None = None,
    deerflow_command: list[str] | None = None,
    deerflow_cwd: str | None = None,
    coding_agent: str = "fake",
    claude_executable: str = "claude",
    codex_executable: str = "codex",
    coding_command: list[str] | None = None,
    coding_cwd: str | None = None,
    compute_command: list[str] | None = None,
    compute_cwd: str | None = None,
    compute_image: str | None = None,
    literature_a2a_url: str | None = None,
    a2a_urls: dict[str, str] | None = None,
    iterations: int = 1,
    objective_metric: str = "score",
    objective_direction: str = "max",
    baseline_command: list[str] | None = None,
    baseline_cwd: str | None = None,
    replicates: int = 1,
    fulltext_paths: list[str] | None = None,
    hypothesis_command: list[str] | None = None,
    hypothesis_cwd: str | None = None,
    analysis_command: list[str] | None = None,
    analysis_cwd: str | None = None,
    reviewer_command: list[str] | None = None,
    reviewer_cwd: str | None = None,
) -> dict[str, object]:
    """Return the non-secret execution choices used to build the agents."""
    compute_command, compute_cwd = _discover_existing_experiment(compute_command, compute_cwd, coding_cwd)
    effective_coding_agent = "command" if coding_agent == "fake" and coding_command else coding_agent
    if not objective_metric or objective_direction not in {"max", "min"}:
        raise ValueError("objective_metric must be non-empty and objective_direction must be 'max' or 'min'")
    return {
        "literature_mode": literature_mode,
        "deepresearch_command": list(deepresearch_command) if deepresearch_command else None,
        "deepresearch_cwd": deepresearch_cwd,
        "deerflow_command": list(deerflow_command) if deerflow_command else ["deerflow", "--json"],
        "deerflow_cwd": deerflow_cwd,
        "literature_a2a_url": literature_a2a_url,
        "a2a_urls": dict(sorted((a2a_urls or {}).items())),
        "iterations": iterations,
        "replicates": replicates,
        "objective_metric": objective_metric,
        "objective_direction": objective_direction,
        "baseline_command": list(baseline_command) if baseline_command else None,
        "baseline_cwd": baseline_cwd,
        "coding_agent": effective_coding_agent,
        "claude_executable": claude_executable,
        "codex_executable": codex_executable,
        "coding_command": list(coding_command) if coding_command else None,
        "coding_cwd": coding_cwd,
        "compute_command": list(compute_command) if compute_command else None,
        "compute_cwd": compute_cwd,
        "compute_image": compute_image,
        "fulltext_paths": list(fulltext_paths or []),
        "hypothesis_command": list(hypothesis_command) if hypothesis_command else None,
        "hypothesis_cwd": hypothesis_cwd,
        "analysis_command": list(analysis_command) if analysis_command else None,
        "analysis_cwd": analysis_cwd,
        "reviewer_command": list(reviewer_command) if reviewer_command else None,
        "reviewer_cwd": reviewer_cwd,
    }


def execution_profile_hash(profile: dict[str, object]) -> str:
    encoded = json.dumps(profile, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_agents(literature_mode: str = "fixture", coding_command: list[str] | None = None, coding_cwd: str | None = None, compute_command: list[str] | None = None, compute_cwd: str | None = None, compute_image: str | None = None, literature_a2a_url: str | None = None, coding_agent: str = "fake", claude_executable: str = "claude", codex_executable: str = "codex", deepresearch_command: list[str] | None = None, deepresearch_cwd: str | None = None, a2a_urls: dict[str, str] | None = None, deerflow_command: list[str] | None = None, deerflow_cwd: str | None = None, baseline_command: list[str] | None = None, baseline_cwd: str | None = None, fulltext_paths: list[str] | None = None, hypothesis_command: list[str] | None = None, hypothesis_cwd: str | None = None, analysis_command: list[str] | None = None, analysis_cwd: str | None = None, reviewer_command: list[str] | None = None, reviewer_cwd: str | None = None):
    compute_command, compute_cwd = _discover_existing_experiment(compute_command, compute_cwd, coding_cwd)
    a2a_urls = dict(a2a_urls or {})
    if literature_a2a_url:
        a2a_urls.setdefault("literature", literature_a2a_url)
    if "literature" in a2a_urls and fulltext_paths:
        raise ValueError("fulltext_paths cannot be combined with a literature A2A endpoint")
    allowed_a2a_stages = {"literature", "hypothesis", "evidence", "coding", "compute", "analysis", "critic", "reviewer", "report"}
    unknown_a2a = sorted(set(a2a_urls) - allowed_a2a_stages)
    if unknown_a2a:
        raise ValueError(f"unknown A2A stages: {', '.join(unknown_a2a)}")
    if coding_agent == "fake" and coding_command:
        coding_agent = "command"
    if coding_agent not in {"fake", "command", "claude", "codex"}:
        raise ValueError("coding_agent must be 'fake', 'command', 'claude' or 'codex'")
    if coding_agent in {"claude", "codex"} and coding_command:
        raise ValueError("coding_command cannot be combined with a CLI coding agent")
    if coding_agent == "command" and not coding_command:
        raise ValueError("coding_command is required when coding_agent=command")
    if literature_mode not in {"fixture", "live", "deepresearch", "deerflow"}:
        raise ValueError("literature_mode must be 'fixture', 'live', 'deepresearch' or 'deerflow'")
    if literature_mode == "deepresearch":
        if literature_a2a_url or not deepresearch_command:
            raise ValueError("deepresearch literature requires deepresearch_command and no literature_a2a_url")
        sources = [DeepResearchSource(deepresearch_command, deepresearch_cwd or ".")]
    elif literature_mode == "deerflow":
        if literature_a2a_url or deepresearch_command:
            raise ValueError("deerflow literature cannot be combined with another literature adapter")
        sources = [DeerFlowSource(deerflow_command, deerflow_cwd or ".")]
    else:
        sources = [FixtureLiteratureSource()] if literature_mode == "fixture" else [CrossrefSource(), ArxivSource()]
    literature = A2AHttpAgent("literature", a2a_urls["literature"]) if "literature" in a2a_urls else LiteratureAgent(sources, fulltext_paths=fulltext_paths)
    if "coding" in a2a_urls:
        if coding_command or coding_agent == "claude":
            raise ValueError("coding A2A endpoint cannot be combined with a local coding adapter")
        coding = A2AHttpAgent("coding", a2a_urls["coding"])
    elif coding_agent == "claude":
        coding = ClaudeCodeAgent(coding_cwd or ".", executable=claude_executable)
    elif coding_agent == "codex":
        coding = CodexCodingAgent(coding_cwd or ".", executable=codex_executable)
    elif coding_agent == "command" and coding_command:
        coding = SubprocessCodingAgent(coding_command, coding_cwd or ".")
    else:
        coding = FakeCodingAgent()
    if "compute" in a2a_urls:
        if compute_command or compute_image or baseline_command:
            raise ValueError("compute A2A endpoint cannot be combined with a local compute adapter")
        compute = A2AHttpAgent("compute", a2a_urls["compute"])
    elif compute_command and compute_image:
        compute = DockerComputeAgent(compute_image, compute_command, compute_cwd or ".", baseline_command=baseline_command, baseline_cwd=baseline_cwd or compute_cwd)
    elif compute_command:
        compute = LocalComputeAgent(compute_command, compute_cwd or ".", baseline_command=baseline_command, baseline_cwd=baseline_cwd or compute_cwd)
    else:
        if baseline_command:
            raise ValueError("baseline_command requires compute_command")
        compute = AutoComputeAgent(coding_cwd) if coding_agent != "fake" and coding_cwd else FakeComputeAgent()
    hypothesis = A2AHttpAgent("hypothesis", a2a_urls["hypothesis"]) if "hypothesis" in a2a_urls else FakeHypothesisAgent()
    if "hypothesis" in a2a_urls and hypothesis_command:
        raise ValueError("hypothesis_command cannot be combined with a hypothesis A2A endpoint")
    if hypothesis_command:
        hypothesis = SubprocessHypothesisAgent(hypothesis_command, hypothesis_cwd or ".")
    evidence = A2AHttpAgent("evidence", a2a_urls["evidence"]) if "evidence" in a2a_urls else ClaimEvidenceAgent()
    analysis = A2AHttpAgent("analysis", a2a_urls["analysis"]) if "analysis" in a2a_urls else MetricsAnalysisAgent()
    if "analysis" in a2a_urls and analysis_command:
        raise ValueError("analysis_command cannot be combined with an analysis A2A endpoint")
    if analysis_command:
        analysis = SubprocessAnalysisAgent(analysis_command, analysis_cwd or ".")
    critic = A2AHttpAgent("critic", a2a_urls["critic"]) if "critic" in a2a_urls else ResearchCriticAgent()
    reviewer = A2AHttpAgent("reviewer", a2a_urls["reviewer"]) if "reviewer" in a2a_urls else EvidenceReviewAgent()
    if "reviewer" in a2a_urls and reviewer_command:
        raise ValueError("reviewer_command cannot be combined with a reviewer A2A endpoint")
    if reviewer_command:
        reviewer = SubprocessReviewerAgent(reviewer_command, reviewer_cwd or ".")
    report = A2AHttpAgent("report", a2a_urls["report"]) if "report" in a2a_urls else ReportWriterAgent()
    return {a.name: a for a in [literature, hypothesis, evidence, coding, compute, analysis, critic, reviewer, report]}


async def run_research(question: str, root: str, literature_mode: str = "fixture", coding_command: list[str] | None = None, coding_cwd: str | None = None, compute_command: list[str] | None = None, compute_cwd: str | None = None, compute_image: str | None = None, literature_a2a_url: str | None = None, auto_approve: bool = True, coding_agent: str = "fake", claude_executable: str = "claude", codex_executable: str = "codex", deepresearch_command: list[str] | None = None, deepresearch_cwd: str | None = None, a2a_urls: dict[str, str] | None = None, deerflow_command: list[str] | None = None, deerflow_cwd: str | None = None, iterations: int = 1, objective_metric: str = "score", objective_direction: str = "max", baseline_command: list[str] | None = None, baseline_cwd: str | None = None, replicates: int = 1, fulltext_paths: list[str] | None = None, hypothesis_command: list[str] | None = None, hypothesis_cwd: str | None = None, analysis_command: list[str] | None = None, analysis_cwd: str | None = None, reviewer_command: list[str] | None = None, reviewer_cwd: str | None = None) -> ResearchTask:
    if not isinstance(iterations, int) or isinstance(iterations, bool) or not 1 <= iterations <= 20:
        raise ValueError("iterations must be between 1 and 20")
    if not isinstance(replicates, int) or isinstance(replicates, bool) or not 1 <= replicates <= 20:
        raise ValueError("replicates must be between 1 and 20")
    store = create_store(root)
    profile = build_execution_profile(literature_mode, deepresearch_command, deepresearch_cwd, deerflow_command, deerflow_cwd, coding_agent, claude_executable, codex_executable, coding_command, coding_cwd, compute_command, compute_cwd, compute_image, literature_a2a_url, a2a_urls, iterations, objective_metric, objective_direction, baseline_command, baseline_cwd, replicates, fulltext_paths, hypothesis_command, hypothesis_cwd, analysis_command, analysis_cwd, reviewer_command, reviewer_cwd)
    task = ResearchTask(question, execution_profile_hash=execution_profile_hash(profile), max_iterations=iterations, replicates=replicates, objective_metric=objective_metric, objective_direction=objective_direction, baseline_requested=bool(baseline_command))
    return await ResearchWorkflow(store, build_agents(literature_mode, coding_command, coding_cwd, compute_command, compute_cwd, compute_image, literature_a2a_url, coding_agent, claude_executable, codex_executable, deepresearch_command, deepresearch_cwd, a2a_urls, deerflow_command, deerflow_cwd, baseline_command, baseline_cwd, fulltext_paths, hypothesis_command, hypothesis_cwd, analysis_command, analysis_cwd, reviewer_command, reviewer_cwd)).run(task, auto_approve=auto_approve)


async def resume_research(task_id: str, root: str, approve: bool = True, literature_mode: str = "fixture", coding_command: list[str] | None = None, coding_cwd: str | None = None, compute_command: list[str] | None = None, compute_cwd: str | None = None, compute_image: str | None = None, literature_a2a_url: str | None = None, coding_agent: str = "fake", claude_executable: str = "claude", codex_executable: str = "codex", deepresearch_command: list[str] | None = None, deepresearch_cwd: str | None = None, a2a_urls: dict[str, str] | None = None, deerflow_command: list[str] | None = None, deerflow_cwd: str | None = None, iterations: int | None = None, objective_metric: str | None = None, objective_direction: str | None = None, baseline_command: list[str] | None = None, baseline_cwd: str | None = None, replicates: int | None = None, fulltext_paths: list[str] | None = None, hypothesis_command: list[str] | None = None, hypothesis_cwd: str | None = None, analysis_command: list[str] | None = None, analysis_cwd: str | None = None, reviewer_command: list[str] | None = None, reviewer_cwd: str | None = None) -> ResearchTask:
    store = create_store(root)
    task = store.get_task(task_id)
    effective_iterations = task.max_iterations if iterations is None else iterations
    effective_replicates = task.replicates if replicates is None else replicates
    effective_metric = task.objective_metric if objective_metric is None else objective_metric
    effective_direction = task.objective_direction if objective_direction is None else objective_direction
    if not isinstance(effective_iterations, int) or isinstance(effective_iterations, bool) or not 1 <= effective_iterations <= 20:
        raise ValueError("iterations must be between 1 and 20")
    if not isinstance(effective_replicates, int) or isinstance(effective_replicates, bool) or not 1 <= effective_replicates <= 20:
        raise ValueError("replicates must be between 1 and 20")
    profile = build_execution_profile(literature_mode, deepresearch_command, deepresearch_cwd, deerflow_command, deerflow_cwd, coding_agent, claude_executable, codex_executable, coding_command, coding_cwd, compute_command, compute_cwd, compute_image, literature_a2a_url, a2a_urls, effective_iterations, effective_metric, effective_direction, baseline_command, baseline_cwd, effective_replicates, fulltext_paths, hypothesis_command, hypothesis_cwd, analysis_command, analysis_cwd, reviewer_command, reviewer_cwd)
    agents = build_agents(literature_mode, coding_command, coding_cwd, compute_command, compute_cwd, compute_image, literature_a2a_url, coding_agent, claude_executable, codex_executable, deepresearch_command, deepresearch_cwd, a2a_urls, deerflow_command, deerflow_cwd, baseline_command, baseline_cwd, fulltext_paths, hypothesis_command, hypothesis_cwd, analysis_command, analysis_cwd, reviewer_command, reviewer_cwd)
    profile_hash = execution_profile_hash(profile)
    if task.execution_profile_hash and task.execution_profile_hash != profile_hash:
        # A dependency-gated task may be resumed after an API restart or a
        # harmless adapter-default change. Keep the operator-approved runtime
        # context usable; explicit profile changes remain rejected elsewhere.
        if task.runtime.get("phase") == "awaiting_dependency_approval" and task.runtime.get("dependency_approval") is True:
            task.execution_profile_hash = profile_hash
        else:
            raise ValueError("execution profile does not match the profile used to create this task")
    if task.execution_profile_hash is None:
        task.execution_profile_hash = profile_hash
    if task.max_iterations != effective_iterations:
        raise ValueError("iterations does not match the profile used to create this task")
    if task.replicates != effective_replicates:
        raise ValueError("replicates does not match the profile used to create this task")
    if task.objective_metric != effective_metric or task.objective_direction != effective_direction:
        raise ValueError("objective does not match the profile used to create this task")
    if task.baseline_requested != bool(baseline_command):
        raise ValueError("baseline command does not match the profile used to create this task")
    return await ResearchWorkflow(store, agents).run(task, auto_approve=approve)


def main() -> None:
    parser = argparse.ArgumentParser(prog="autoresearch")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--store", default=".autoresearch")
    parser.add_argument("--literature", choices=("fixture", "live", "deepresearch", "deerflow"), default="fixture")
    parser.add_argument("--deepresearch-command", type=parse_command, help="structured DeepResearch command; receives JSON on stdin")
    parser.add_argument("--deepresearch-cwd", help="working directory for --deepresearch-command")
    parser.add_argument("--deerflow-command", type=parse_command, help="DeerFlow command prefix; defaults to 'deerflow --json'")
    parser.add_argument("--deerflow-cwd", help="working directory for --deerflow-command")
    parser.add_argument("--literature-a2a-url", help="remote A2A HTTP+JSON endpoint for the literature Agent")
    parser.add_argument("--a2a-agent", action="append", metavar="STAGE=URL", help="route any stage to an A2A endpoint; may be repeated")
    parser.add_argument("--resume", help="resume a persisted task from AWAITING_APPROVAL")
    parser.add_argument("--no-approve", action="store_true", help="leave a resumed task at AWAITING_APPROVAL")
    parser.add_argument("--coding-command", type=parse_command, help="quoted external coding command; receives JSON on stdin")
    parser.add_argument("--coding-agent", choices=("fake", "command", "claude", "codex"), default="fake")
    parser.add_argument("--claude-executable", default="claude")
    parser.add_argument("--codex-executable", default="codex")
    parser.add_argument("--coding-cwd", help="working directory for --coding-command")
    parser.add_argument("--compute-command", type=parse_command, help="quoted experiment command; must print JSON metrics")
    parser.add_argument("--compute-cwd", help="working directory for --compute-command")
    parser.add_argument("--compute-image", help="Docker image for isolated compute; requires --compute-command")
    parser.add_argument("--iterations", type=int, help="number of coding/compute/analyze iterations (1-20)")
    parser.add_argument("--replicates", type=int, default=1, help="independent candidate runs per iteration (1-20)")
    parser.add_argument("--fulltext", action="append", dest="fulltext_paths", default=[], help="explicit local HTML/TXT/MD/PDF full-text file; may be repeated")
    parser.add_argument("--hypothesis-command", type=parse_command, help="quoted external hypothesis command; receives JSON on stdin and returns hypotheses JSON")
    parser.add_argument("--hypothesis-cwd", help="working directory for --hypothesis-command")
    parser.add_argument("--analysis-command", type=parse_command, help="quoted external analysis command; returns Finding JSON")
    parser.add_argument("--analysis-cwd", help="working directory for --analysis-command")
    parser.add_argument("--reviewer-command", type=parse_command, help="quoted external reviewer command; returns ReviewReport JSON")
    parser.add_argument("--reviewer-cwd", help="working directory for --reviewer-command")
    parser.add_argument("--objective-metric")
    parser.add_argument("--objective-direction", choices=("max", "min"))
    parser.add_argument("--baseline-command", type=parse_command, help="baseline experiment command; must print JSON metrics")
    parser.add_argument("--baseline-cwd", help="working directory for --baseline-command")
    args = parser.parse_args()
    a2a_urls = {}
    for item in args.a2a_agent or []:
        if "=" not in item:
            parser.error("--a2a-agent must use STAGE=URL")
        stage, url = item.split("=", 1)
        if not stage or not url:
            parser.error("--a2a-agent must use STAGE=URL")
        a2a_urls[stage] = url
    if args.resume:
        task = asyncio.run(resume_research(args.resume, args.store, approve=not args.no_approve, literature_mode=args.literature, coding_command=args.coding_command, coding_cwd=args.coding_cwd, compute_command=args.compute_command, compute_cwd=args.compute_cwd, compute_image=args.compute_image, literature_a2a_url=args.literature_a2a_url, coding_agent=args.coding_agent, claude_executable=args.claude_executable, codex_executable=args.codex_executable, deepresearch_command=args.deepresearch_command, deepresearch_cwd=args.deepresearch_cwd, a2a_urls=a2a_urls, deerflow_command=args.deerflow_command, deerflow_cwd=args.deerflow_cwd, iterations=args.iterations, objective_metric=args.objective_metric, objective_direction=args.objective_direction, baseline_command=args.baseline_command, baseline_cwd=args.baseline_cwd, replicates=args.replicates if args.replicates != 1 else None, fulltext_paths=args.fulltext_paths, hypothesis_command=args.hypothesis_command, hypothesis_cwd=args.hypothesis_cwd, analysis_command=args.analysis_command, analysis_cwd=args.analysis_cwd, reviewer_command=args.reviewer_command, reviewer_cwd=args.reviewer_cwd))
    elif args.question:
        task = asyncio.run(run_research(args.question, args.store, args.literature, args.coding_command, args.coding_cwd, args.compute_command, args.compute_cwd, args.compute_image, args.literature_a2a_url, coding_agent=args.coding_agent, claude_executable=args.claude_executable, codex_executable=args.codex_executable, deepresearch_command=args.deepresearch_command, deepresearch_cwd=args.deepresearch_cwd, a2a_urls=a2a_urls, deerflow_command=args.deerflow_command, deerflow_cwd=args.deerflow_cwd, iterations=args.iterations or 1, objective_metric=args.objective_metric or "score", objective_direction=args.objective_direction or "max", baseline_command=args.baseline_command, baseline_cwd=args.baseline_cwd, replicates=args.replicates, fulltext_paths=args.fulltext_paths, hypothesis_command=args.hypothesis_command, hypothesis_cwd=args.hypothesis_cwd, analysis_command=args.analysis_command, analysis_cwd=args.analysis_cwd, reviewer_command=args.reviewer_command, reviewer_cwd=args.reviewer_cwd))
    else:
        parser.error("question is required unless --resume is supplied")
    report_path = _write_human_report(task, args.store)
    print(json.dumps({"task_id": task.task_id, "state": task.state, "artifacts": task.artifacts, "history": task.history, "error": task.error, "report_path": report_path}, indent=2))


if __name__ == "__main__":
    main()
