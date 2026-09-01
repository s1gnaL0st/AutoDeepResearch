# AutoDeepResearch

![AutoDeepResearch console](docs/assets/research-console.png)
![Uploading image.png…]()

Artifact-first orchestration core for reproducible end-to-end research.

## Current milestone: MVP-3

The repository contains a zero-dependency local workflow with:

- a guarded research state machine;
- versioned, content-hashed Artifact records;
- a transport-neutral A2A message envelope;
- replaceable Agent contracts;
- a source-snapshotting Crossref/arXiv literature Agent, plus an offline fixture source;
- replaceable hypothesis, coding, compute, analysis, review and report agents;
- a local append-only JSON store and CLI;
- a loopback control-plane API with task-owned Artifact reads, bounded jobs and
  cancellation-safe completion handling.

Crossref/arXiv and DeepResearch-compatible sources are supported for retrieval;
Claude Code can be selected as the coding agent. Hypothesis, analysis, review and
reporting remain deliberately conservative by default and still require domain
evaluation suites and human scientific review.

## RAG and PostgreSQL/pgvector

When `AUTORESEARCH_DATABASE_URL` is set, full-text passages are chunked,
embedded, and persisted in PostgreSQL. With the `vector` extension installed,
the store uses a `vector(256)` column, HNSW cosine index, and a hybrid query
(70% vector cosine + 30% PostgreSQL full-text score). Chunks created before the
extension was enabled are automatically backfilled into the vector column.
Without the extension the runtime reports `postgres_jsonb_compat` and performs
the same deterministic scoring in Python; it never labels this as native
pgvector.

The default `hashing-256-offline-baseline` embedder is dependency-free and is
intended for smoke tests. For a local semantic model, install
`sentence-transformers` into the chosen environment and set
`AUTORESEARCH_EMBEDDING_MODEL` to a cached 256-dimensional model. Model
downloads are never implicit. If the package/model is unavailable or its
dimension is not 256, the EvidenceSet records the configuration error and
uses the explicit hashing fallback.

Example PostgreSQL configuration (all paths may remain on a non-system drive):

```powershell
$env:AUTORESEARCH_DATABASE_URL = "postgresql://postgres:***@127.0.0.1:5432/autoresearch"
```

The extension itself must be installed by a PostgreSQL administrator. On
Windows, place the compiled files under the PostgreSQL installation directory
(for this project, `D:\PostgreSQL`) and then run `CREATE EXTENSION vector;`.
The project does not install PostgreSQL, compilers, or models on `C:`.

## Run

```powershell
$env:PYTHONPATH = "src"
python -m autoresearch.cli "Does X improve Y?" --store .autoresearch
```

## Local product console

The repository includes a dependency-free local web console in `frontend/`.
Run the API and static UI in two terminals:

```powershell
$env:PYTHONPATH = "src"
python -m autoresearch.api --store .autoresearch --port 8090
python -m http.server 5173 --directory frontend
```

Then open <http://127.0.0.1:5173>. The console creates missions, shows the
research pipeline as it advances, and lets you inspect immutable Artifacts.
Writer and Reviewer remain local; no A2A endpoint is required for the UI.

The default `fixture` source is deterministic. To query public metadata services, opt in explicitly:

```powershell
python -m autoresearch.cli "causal representation learning" --literature live --store .autoresearch
```

An external DeepResearch project can also be wired in without changing the
workflow. Its command receives `{"query":"...","limit":5}` on stdin and must
print `{"records":[...]}` or `{"papers":[...]}` on stdout. Each record requires
at least a title and may include `authors`, `year`, `doi`, `arxiv_id`, `url`,
`venue`, and `abstract`.

```powershell
python -m autoresearch.cli "question" --literature deepresearch `
  --deepresearch-command "python path/to/deepresearch_adapter.py" `
  --deepresearch-cwd .
```

DeerFlow has a dedicated adapter for its headless
`deerflow --json "question"` newline-delimited StreamEvents. It extracts only
explicit `[citation:Title](URL)` links and keeps the complete event stream as a
hashed source snapshot:

```powershell
python -m autoresearch.cli "question" --literature deerflow `
  --deerflow-command "deerflow --json" --deerflow-cwd .
```

The default command prefix is `deerflow --json`; authentication and DeerFlow
tool permissions stay under the external project's configuration.

The command prints the task id, final state, transition history and Artifact ids. JSON files are written beneath `.autoresearch/tasks` and `.autoresearch/artifacts`.
When a literature source returns structured DeerFlow intelligence, the store
also contains a `LiteratureIntelligence` Artifact and the readable sidecars
`paper_cards.md`, `comparison_matrix.md`, `innovation_brief.md`, and
`research_plan.md`.
The same run also emits `manuscript.md`, a Nature-style evidence-first
manuscript skeleton with explicit `AUTHOR_INPUT_NEEDED` placeholders. The
report Artifact records the writing, reviewer and statistics profiles used;
these are rules applied locally, not a claim that an external Nature service
was invoked.
When DeerFlow supplies a non-empty plan, the workflow additionally persists a
`ResearchPlan` Artifact; Hypothesis and later Coding stages can consume its
baseline, candidate, metric, failure condition and resource budget fields.

## Workflow contract

```text
DRAFT -> SEARCHING -> EVIDENCE_READY -> HYPOTHESES_READY
      -> AWAITING_APPROVAL -> IMPLEMENTING -> RUNNING
      -> ANALYZING -> ITERATING -> IMPLEMENTING ...
      -> ANALYZING -> REVIEWING -> REPORT_READY

Any non-terminal stage can transition to CANCELLED when the control plane
observes a persisted cancellation request at an Agent boundary.
```

Use `--iterations N` (1-20) to run the coding/compute/analysis loop repeatedly.
Use `--replicates N` (1-20) to execute N independent candidate runs per
iteration. The analysis Artifact reports mean, sample standard deviation,
standard error and `n`; the objective ledger and baseline delta use the
iteration mean, while the reproducibility package and report retain every
replicate's raw Artifact and metric trajectory. These are descriptive
statistics, not an inferential test.
For `n > 1`, the analysis also records a descriptive 95% normal-approximation
confidence interval and baseline delta/effect-size field; these do not replace
domain-appropriate statistical testing.
Each iteration receives the previous artifacts through the A2A message envelope;
the final report and reproducibility package retain the full metric trajectory.
When `--iterations` is greater than 1, a local `ResearchCriticAgent` runs after
each analysis and emits a `ResearchDecision`. A non-improving result can trigger
`revise_hypotheses`, which regenerates the hypothesis set and claim-evidence map
before the next coding iteration. The loop is always bounded by `--iterations`.
An A2A Critic may also return `stop_early` to end a low-value branch before its
iteration budget is exhausted; the workflow then proceeds directly to review.
Set `--objective-metric NAME --objective-direction max|min` to define the
optimization ledger. The controller records the best observed value only when
that numeric metric is actually present; it never infers improvement from prose.
Optionally provide `--baseline-command` alongside `--compute-command`. The
baseline runs before the first candidate, enters the provenance graph as a
separate `ExperimentRun`, and enables a descriptive `delta_vs_baseline` in the
analysis Artifact:

```powershell
python -m autoresearch.cli "question" `
  --compute-command "python experiment.py" `
  --baseline-command "python baseline.py" `
  --objective-metric score --objective-direction max `
  --iterations 3 --replicates 3
```

Baseline and candidate commands must emit the same numeric metric schema. The
controller reports deltas but does not treat them as causal or statistically
significant effects.

Every Agent receives an `A2AMessage` and returns one Artifact. A real adapter can replace a Fake agent without changing the workflow contract. The control plane validates the expected Artifact kind at every stage; a malformed or misrouted response produces a failed `AgentContractViolation` Artifact and stops the task before later stages can consume it. Literature evidence contains normalized DOI/arXiv identifiers, deduplicated records, metadata-verification status and source-response snapshots with hashes. When an abstract is available, the EvidenceSet also stores hashable candidate passages with explicit `support_status=candidate`; this is retrieval provenance, not claim verification. Metadata verification proves an identifier match only; the built-in Evidence Mapper adds lexical full-text candidates, while semantic claim-to-passage verification remains a human responsibility. A search with no records stops the workflow instead of producing a report.

Explicit local `.html`, `.htm`, `.txt`, `.md`, and `.pdf` full text may be
attached with repeatable `--fulltext PATH`. The literature Agent hashes source
bytes and emits passages with paragraph, section, or PDF-page locators. The
built-in `ClaimEvidenceMap` uses lexical matching only and marks every hit as
unverified until human entailment and citation review. The project never
downloads or modifies those files; obtain them through lawful OA or
user-authorized access.

The coding boundary is available through `SubprocessCodingAgent`: an explicit command receives a JSON request on stdin and returns a content-hashed `CodeRevision` Artifact. The request includes both input Artifact IDs and their structured content, so an iterative coding agent can inspect the previous Finding and experiment context. For a Git working tree, every CodeRevision also records before/after HEAD, status, and worktree/staged diff hashes; it does not store a mutable checkout copy. It uses no shell, requires an existing working directory, captures stdout/stderr and enforces a timeout. Example:

```powershell
python -m autoresearch.cli "question" --coding-command "python agent_worker.py" --coding-cwd .
```

External commands are opt-in; the default remains the deterministic Fake coding agent.

Hypothesis generation has the same structured subprocess boundary. Use
`--hypothesis-command` with a command that reads the JSON request from stdin
and prints `{"hypotheses":[{"id":"H1","statement":"..."}]}`. Responses are
validated for unique ids and non-empty statements before they enter the
provenance graph; malformed or narrative-only output is rejected.

Analysis and review can also be delegated to explicit subprocess adapters. The
analysis command must return a JSON object with a non-empty `finding` and a
conservative `confidence` (`descriptive_only`, `hypothesis_only`, or
`human_review_required`). The reviewer command must return `decision` as
`requires_human_review`, `pass_with_human_review`, or `blocked`, plus optional
string arrays for `blocking_issues`, `scientific_limitations`, and
`reproducibility`. Raw request/stdout/stderr and the normalized response are
stored in the resulting Artifact for auditability:

```powershell
python -m autoresearch.cli "question" `
  --analysis-command "python analysis_adapter.py" --analysis-cwd . `
  --reviewer-command "python reviewer_adapter.py" --reviewer-cwd .
```

The same fields are accepted by `POST /research` and resume endpoints as
`analysis_command`, `analysis_cwd`, `reviewer_command`, and `reviewer_cwd`.
External commands are opt-in and are never executed unless explicitly supplied.

For a directly installed Claude Code CLI, use the dedicated adapter. It invokes
`claude -p ... --output-format json` without adding a permission bypass flag:

```powershell
python -m autoresearch.cli "question" --coding-agent claude --coding-cwd .
```

The executable can be overridden with `--claude-executable`; authentication and
the Claude Code permission policy remain outside this project.

For an installed OpenAI Codex CLI, use the Codex adapter. It invokes
`codex exec --json --sandbox workspace-write` in the supplied coding workspace;
the CLI's own authentication (including a `CODEX_API_KEY` environment
variable, when required by the configured provider) stays outside this
project:

```powershell
python -m autoresearch.cli "question" --coding-agent codex `
  --codex-executable codex --coding-cwd .
```

The adapter records the raw JSONL response and before/after Git workspace
provenance, but never includes credentials in the prompt or Artifact payload.

`LocalComputeAgent` and `DockerComputeAgent` are execution runners, not extra
LLM Agents. Codex (or another Coding Agent) writes and debugs the experiment,
then the Orchestrator invokes one of these runners to obtain an auditable
`ExperimentRun`. An experiment command must print either a JSON object such as
`{"metrics":{"accuracy":0.91}}` or a line prefixed with
`AUTORESEARCH_RESULT:`. Missing/invalid metrics, non-zero exits and timeouts
produce failed `ExperimentRun` Artifacts. Docker mode adds no network,
CPU/memory/PID limits, a read-only root filesystem, a read-only workspace
mount, dropped Linux capabilities and a bounded temporary directory:

```powershell
python -m autoresearch.cli "question" --compute-command "python experiment.py" --compute-cwd .
python -m autoresearch.cli "question" --compute-command "python experiment.py" --compute-cwd . --compute-image autoresearch/experiment:latest
```

## A2A integration

The project now has an A2A v1 HTTP+JSON development adapter. `A2AAgentServer` publishes an Agent Card at `/.well-known/agent-card.json` and accepts `POST /message:send`; `A2AHttpAgent` discovers that card and exchanges structured request and Artifact data through A2A `Part.data`. The client recomputes every remote Artifact content hash and rejects a mismatched response before persistence. The bundled server is loopback-only and has no authentication, so it is for local development and interoperability tests only. A production deployment must add HTTPS, authentication and authorization before exposing an Agent Card publicly.

Remote task failures are retained as failed Artifacts when the peer returns an
A2A failure state, including the peer's error metadata. If the returned kind
does not satisfy the local stage contract, the original remote failure and its
linked `AgentContractViolation` are both retained; the workflow still stops and
never turns that response into a successful report.

The workflow can route any stage to a compliant AutoResearch A2A endpoint. The
legacy literature-specific option remains available:

```powershell
python -m autoresearch.cli "question" --literature-a2a-url http://127.0.0.1:8080
```

For other stages, use repeatable `STAGE=URL` arguments:

```powershell
python -m autoresearch.cli "question" `
  --a2a-agent coding=http://127.0.0.1:8081 `
  --a2a-agent compute=http://127.0.0.1:8082
```

Valid stages are `literature`, `hypothesis`, `coding`, `compute`, `analysis`,
`evidence`, `reviewer`, and `report`. Local adapters remain the defaults for any stage not
listed.

## Reproducibility package

Before a task reaches `REPORT_READY`, the control plane writes a `ReproducibilityPackage` Artifact. It verifies each earlier Artifact's content hash and input references, records the evidence summary, latest code provenance, experiment metrics, review, claim-to-evidence links and any replayable command. Retried tasks retain their complete historical manifest, while the summary fields select the latest attempt's evidence and review Artifacts. Artifact storage is immutable by ID: replaying the identical record is idempotent, while any same-ID record with different content is rejected rather than overwriting provenance. Integrity failure prevents a successful final state. The final `ResearchReport` references the evidence, experiment, finding, review and reproducibility artifacts and is marked `draft_for_human_review`.

## Approval and resume

The state machine persists at `AWAITING_APPROVAL`. A controller can start a paused run with `run_research(..., auto_approve=False)` and resume it by task id:

```powershell
python -m autoresearch.cli --resume <task-id> --store .autoresearch
```

When resuming an external coding, compute or A2A configuration, pass the same explicit adapter options used to create the task. This keeps execution authority visible in the command line rather than storing credentials or arbitrary commands in task state. Queue retries retain prior attempt Artifacts and their failure records for auditability, but restart the literature stage with no old Artifact inputs so stale failure data is not reused as research evidence.

`REPORT_READY` and `CANCELLED` are idempotent terminal states: a repeated
workflow invocation returns the persisted task rather than rerunning Agents or
rewriting its history.

### Dependency installation is a human gate

Before a local experiment is executed, the Compute Agent checks the
experiment workspace's explicit `requirements.txt` with the same interpreter
that will run the command. Missing packages never trigger a silent download.
The task enters `AWAITING_DEPENDENCY_APPROVAL` and exposes the missing
distributions and requirements path in `task.runtime`. Approve by resuming
with `{"approve_dependencies": true}`; only then does the worker run
`python -m pip install -r requirements.txt` and retry the current experiment
iteration. A denial is persisted as a pause and can be resumed later with a
new approval decision:

```text
POST /research/{task_id}/resume
{"approve_dependencies": false}
```

This gate is separate from the research-plan approval: it authorizes a
network/package-environment mutation, while the normal approval authorizes
continuing the research workflow.

## Control-plane API

`ResearchApiServer` exposes a loopback-only HTTP API for integrating a UI or a
queue controller. New tasks default to `AWAITING_APPROVAL`; set
`auto_approve: true` only for trusted automation.

The workflow checks the persisted cancellation flag before and after every
Agent call, then transitions to `CANCELLED` without starting the next stage.
The queue also re-reads that flag after a worker callback returns, so a
cancellation arriving during a long external command cannot be overwritten by
a stale in-memory success result. A currently running external process is not
force-killed; the request takes effect at the next Agent boundary.

```python
from autoresearch.api import ResearchApiServer

with ResearchApiServer(".autoresearch", port=8090):
    ...
```

Endpoints:

- `GET /research` returns compact persisted-task summaries for a UI or queue
  dashboard. It accepts repeatable `state=...` and `execution_status=...`
  filters, for example `/research?state=AWAITING_APPROVAL`.
- `POST /research` with `{"question":"..."}` creates a persisted task.
- `GET /research/{task_id}` returns the task and artifact ids.
- `GET /research/{task_id}/status` returns a consolidated read-only status
  summary with latest evidence, Finding, review, report, iteration objective,
  and most recent failure references.
- `GET /research/{task_id}/artifacts` returns the task's full Artifact records
  in provenance order; `GET /research/{task_id}/artifacts/{artifact_id}` reads
  one task-owned Artifact.
- `POST /research/{task_id}/approve` resumes a paused task.
- `POST /research/{task_id}/resume` resumes with an explicit `approve` flag.
- `POST /research/{task_id}/resume` with `approve_dependencies: true|false`
  handles the local experiment dependency-installation gate. A false decision
  leaves the task resumable and records the denial; it never marks the
  experiment as having produced metrics.
- `POST /research/{task_id}/retry` explicitly resubmits an `orphaned` or
  `FAILED` task to the local queue. Supply the same external Agent profile used
  for the original task; profiles are intentionally not persisted.
- `GET /research/{task_id}/job` returns the persisted background job state.
- `POST /research/{task_id}/cancel` requests cancellation of an active job.
- `POST /research/{task_id}/adjudicate` records human decisions over the
  generated claim-to-evidence candidates. The body is
  `{"adjudicator":"...","decisions":[{"claim_id":"H1","passage_id":"...","decision":"supported|rejected|uncertain","note":"..."}]}`.
  It only accepts candidate passage ids from that task's immutable
  `ClaimEvidenceMap`, and creates new `EvidenceAdjudication`,
  `ReproducibilityPackage`, and report Artifacts without altering the original
  retrieval evidence. A `supported` decision is a recorded human judgment,
  not an automatic proof of truth or causality.

For long-running work, submit with `{"async":true,"auto_approve":true}`. The
API first establishes the evidence/approval checkpoint, then returns `202` and
queues coding, compute, analysis and reporting. `max_attempts` may be set from
1 to 5. A process restart marks queued/running jobs as `orphaned`; they are not
executed blindly because their execution profile is intentionally not stored.
Use the retry endpoint with an explicit profile to resume an orphaned task;
previous attempt Artifacts stay in the provenance graph.
Set `iterations` from 1 to 20 in the same request to control the experiment loop.
Set `replicates` from 1 to 20 to control independent runs per iteration. Set
`fulltext_paths` to an array of explicitly supplied local full-text paths to
run the same provenance extraction and claim-mapping flow as `--fulltext`.

The API is a local integration surface, not a production server: it has no
authentication or TLS, and its worker pool is intentionally single-process.

The queue itself is a bounded in-process worker pool with atomic per-task lock
files. It is a reliable local integration step, not yet a replacement for a
durable broker/worker system such as Temporal or Celery.

Run it directly with:

```powershell
$env:PYTHONPATH = "src"
python -m autoresearch.api --store .autoresearch --port 8090
```
