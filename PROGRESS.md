# AutoResearch Progress Log

This file records each substantive implementation and verification step. Entries
state what changed, how it was checked, and the next concrete action so the
project can be resumed without relying on chat history.

## 2026-08-30 - Human approval gate for experiment dependencies

- Change: Local Compute now preflights an experiment workspace's explicit
  `requirements.txt` with the interpreter that will run the experiment.
- Change: Missing packages produce `ExperimentRun(status=requires_approval)`
  and move the task to `AWAITING_DEPENDENCY_APPROVAL`; no experiment command
  or package download runs before approval.
- Change: `POST /research/{task_id}/resume` accepts
  `approve_dependencies: true|false`. Approval installs the requirements and
  retries the current iteration; denial persists a resumable pause with the
  missing package list and the denial reason.
- Change: The console exposes a dependency review action and asks again on
  Resume after a denial.
- Verification: `tests/test_dependency_approval.py` plus `tests/test_mvp.py`
  pass (`86 passed`); frontend JavaScript syntax check and Python compile
  check pass. The full repository pytest command remains intentionally
  excluded because it collects unrelated DeerFlow third-party tests.

## 2026-08-30 - Nested model metrics and failure detail

- Fix: Experiment output parsing now extracts scalar metrics from model objects
  that also contain nested confusion matrices/per-class details.
- Fix: Artifact inspector shows failure reason before bounded raw payload.
- Verification: MVP suite passes (`82 passed`); frontend syntax passes.

## 2026-08-30 - DeerFlow initialization hang fixed and verified

- Fix: DeerFlow adapter now invokes local CLI as `--recursion-limit 30 --print`
  (correct message placement), forces UTF-8 output, and parses nested tagged
  JSON with a greedy bounded closing-tag match.
- Fix: Structured DeerFlow plans with zero citation cards are preserved as
  evidence provenance instead of being silently discarded.
- Verification: Direct DeerFlow search returned DOI records; full API flow with
  `literature=deerflow` reached `REPORT_READY` after real initialization and
  completed all downstream stages.

## 2026-08-30 - UI artifact rendering smoke test

- Fix: Artifact inspection now caps rendered JSON at 24,000 characters and uses
  a `<pre>` block with producer/size metadata, preventing large DeerFlow
  EvidenceSet payloads from freezing the browser main thread.
- Verification: Real API workflow smoke (`POST /research` → poll task → list
  artifacts) reached `REPORT_READY` with 9 artifacts; frontend syntax and MVP
  tests pass (`82 passed`).

## 2026-08-29 - DeerFlow + Terra Codex end-to-end smoke test passed

- Fix: DeerFlow local adapter uses argument order `--recursion-limit 30 --print`
  (the previous order dropped the prompt), forces UTF-8 subprocess output, and
  parses citation URLs from plain `--print` responses as well as JSONL streams.
- Verification: Real run completed `DRAFT → SEARCHING → EVIDENCE_READY →
  HYPOTHESES_READY → IMPLEMENTING → RUNNING → ANALYZING → REVIEWING →
  REPORT_READY` using DeerFlow and Codex `gpt-5.6-terra`; ExperimentRun was
  local and returned accuracy `0.9861111111` / macro-F1 `0.9858906412`.

## 2026-08-29 - Pin Codex CLI smoke tests to Terra

- Change: Codex coding adapter now passes `--model gpt-5.6-terra` explicitly,
  preventing the CLI default (for example Luna) from being selected.

## 2026-08-29 - Align DeerFlow relay endpoint

- Fix: Updated the local DeerFlow model configuration to use the user-provided
  relay endpoint `https://sorryios.ai/codex` instead of the stale host.
- Verification: DeerFlow CLI responds to a simple request; research prompts
  remain subject to model/search tool availability and timeout limits.

## 2026-08-29 - Auto-detect local DeerFlow CLI

- Fix: When no DeerFlow command is supplied, the adapter now checks the selected
  checkout for `backend/.venv/Scripts/deerflow.exe` before falling back to PATH.
- Verification: Existing MVP tests remain green.

## 2026-08-29 - Mission directories and optional file deletion

- Change: Each persisted Mission now receives a sanitized directory under
  `.autoresearch/missions/<question>-<id>`.
- Change: Deletion asks whether to remove that local directory; task metadata,
  artifacts and jobs are always removed, while local files are retained by
  default.
- Verification: MVP suite passes (`82 passed`); frontend syntax passes.

## 2026-08-29 - Task deletion

- Change: Added explicit DELETE action in the mission detail view with a
  confirmation prompt.
- Change: Added `/research/{id}/delete`; it removes the task, owned artifacts,
  and job records, while refusing deletion of queued/running jobs.
- Verification: MVP suite passes (`82 passed`); frontend syntax passes.

## 2026-08-29 - Guard pause on inactive jobs

- Fix: PAUSE now checks the task Job status in the UI and gives a clear message
  when no queued/running job exists, instead of surfacing a backend exception.
- Verification: Frontend JavaScript syntax check passes; UI restarted.

## 2026-08-29 - Pause and resume control

- Change: Added persisted `PAUSED` state with pause request/checkpoint fields.
- Change: Queue now supports pause requests, records paused jobs, and resumes
  through the safe approval checkpoint without replaying a partial subprocess.
- Change: Added `/research/{id}/pause` and `/research/{id}/resume` controls and
  a PAUSE/RESUME button in the mission detail view.
- Verification: MVP suite passes (`82 passed`); frontend syntax passes.

## 2026-08-29 - Make live literature the default

- Fix: New missions now default to Live metadata retrieval; Fixture literature
  remains available as an explicit demo option.
- Change: Runtime guidance no longer hard-codes a `score` output and points to
  automatic metric selection.
- Verification: Frontend JavaScript syntax check passes.

## 2026-08-30 - Agent benchmark memo

- Added `BENCHMARK_MEMO.md` documenting external Agent/Coding/Web/Research
  benchmarks and a proposed local `AutoResearch-Bench` task suite.

## 2026-08-30 - Coding-to-Compute handoff

- Change: Added `AutoComputeAgent`; real Coding Agent runs now discover the
  generated experiment entry point after coding and execute it automatically.
- Change: Supports an optional `execution_contract` and reports an explicit
  failure when no contract or known entry file exists; Fake mode remains the
  only source of synthetic `0.81` results.
- Verification: MVP suite passes (`82 passed`).

## 2026-08-30 - Dependency approval resume fix

- Fix: Dependency approval is now propagated into workflow message parameters,
  so an approved install is not gated again on the next run.
- Fix: The resume action recognizes the durable dependency-gate runtime phase
  even if the task state is still transitioning through `IMPLEMENTING`.
- Verification: Frontend syntax check and MVP suite pass (`82 passed`).

## 2026-08-30 - Dependency failure diagnostics

- Fix: Compute recognizes runner messages that explicitly report missing
  dependencies (for example, `requires torch and torchvision`) and records an
  environment remediation hint in the ExperimentRun artifact.
- Diagnosis: Task `59b6ebc6-7469-4ca5-866d-8784491351c6` generated its own
  failed ExperimentRun; this was not a previous task's artifact being reused.
- Verification: MVP suite passes (`82 passed`).

## 2026-08-30 - Correct orphan task state

- Fix: When a worker disappears, the task is now transitioned to explicit
  `FAILED` while retaining `execution_status=orphaned` and the recovery error.
- Result: The UI no longer shows an active spinner or misleading `IMPLEMENTING`
  state; `RETRY` remains available and can resume from existing artifacts.
- Verification: MVP suite passes (`82 passed`).

## 2026-08-30 - Environment diagnostics and mission timestamps

- Fix: Compute records missing Python modules (including pip visibility and an
  install remediation) in failed ExperimentRun artifacts.
- Change: Mission list now displays each task's creation timestamp.
- Verification: Frontend syntax check and MVP suite pass (`82 passed`).

## 2026-08-30 - Runtime progress metadata

- Change: Experiment runs now persist runtime phase, command, workspace,
  start/finish timestamps, iteration/replicate and the latest output excerpt.
- Change: `/status` exposes this runtime metadata for live monitoring and
  post-failure diagnosis.
- Verification: MVP suite passes (`82 passed`).

## 2026-08-30 - Expose mission creation time

- Fix: Added durable `created_at` to `ResearchTask` and returned it in list
  summaries; the frontend can now display creation time for every mission.
- Verification: MVP suite passes (`82 passed`); services restarted.

## 2026-08-30 - Stable creation timestamps

- Fix: Task storage now restores persisted `created_at` instead of creating a
  new timestamp on every list read. Legacy task files use their filesystem
  creation time as a stable backfill value.
- Verification: API task listing returns stable timestamps after service restart.

## 2026-08-30 - Flexible agent-driven experiment recovery

- Change: AutoCompute recursively discovers external datasets instead of
  assuming one fixed directory layout.
- Change: Retry Coding Agent receives prior `ExperimentRun` failure artifacts
  and is explicitly instructed to diagnose and repair entry-point arguments.
- Verification: MVP suite passes (`82 passed`).

## 2026-08-30 - Required external-test discovery

- Fix: AutoCompute now detects argparse-required `--external-test` runners and
  selects only valid dataset paths (`external_test.npz` or `external/`).
- Fix: Missing datasets produce a clear failure reason rather than invoking the
  runner without its required argument.
- Verification: MVP suite passes (`82 passed`).

## 2026-08-30 - Stage-aware retry

- Fix: Failed tasks with an existing `CodeRevision` now retry from
  `IMPLEMENTING`, preserving literature evidence and hypotheses instead of
  restarting the full search workflow.
- Verification: MVP suite passes (`82 passed`).

## 2026-08-30 - Auto-discovered runner arguments

- Fix: AutoCompute now supplies `--external-test test_experiment.py` for the
  common generated `run_experiment.py` + co-located test layout.
- Verification: MVP suite passes (`82 passed`).

## 2026-08-30 - Fix mission deletion request

- Fix: DELETE requests with the `delete_files` option were incorrectly rejected
  by generic execution-profile validation before reaching the delete handler.
- Verification: API deletion now succeeds and returns `files_deleted`; MVP
  suite remains green (`82 passed`).

## 2026-08-29 - Objective metric can be agent-selected

- Change: The UI now exposes METRIC clearly and defaults to `auto`.
- Change: The workflow infers a numeric objective (preferring `accuracy`) from
  the first successful run and persists it for later iterations/reporting.
- Verification: MVP suite passes (`82 passed`).

## 2026-08-29 - Accept model-comparison experiment output

- Fix: Local compute accepts canonical `metrics` JSON and common `models`
  comparison JSON, selecting the best model for the configured objective.
  `score` aliases `accuracy` when appropriate.
- Verification: Nested-output coverage added; MVP suite passes (`82 passed`).

## 2026-08-29 - Prevent synthetic runs being reported as real

- Fix: `ReportWriterAgent` now classifies runs from persisted execution
  provenance (`executor`, `environment`, `command`, `stdout`, `returncode`).
  Legacy artifacts with empty execution fields are treated as synthetic demo
  output instead of being labelled real.
- Fix: `FakeComputeAgent` explicitly records `executor=fake`,
  `environment=synthetic`, and null command/returncode fields.
- Verification: Added fake/legacy and local-run coverage; MVP suite passes
  (`81 passed`).

## 2026-08-29 - Expose fixture literature provenance

- Change: Reports now include `evidence_mode` and explicitly warn when the
  deterministic FixtureLiteratureSource was used. This prevents a real
  experiment paired with placeholder literature from being mistaken for a
  fully evidence-grounded study.
- Verification: MVP suite remains green (`81 passed`).

## 2026-08-28 - Progress logging introduced

- Status: in progress.
- Change: Added this durable project progress log at the user's request.
- Scope: Continue the artifact-first AutoResearch control plane toward a usable
  end-to-end research workflow while preserving human approval and scientific
  review gates.
- Evidence: The current repository contains the MVP-3 workflow, A2A adapters,
  external Agent boundaries, local control-plane API, and a unit test suite.
- Next: Establish a fresh test baseline, audit the highest-impact remaining
  end-to-end gap, then implement and verify it.

## 2026-08-28 - Baseline verification attempt

- Status: interrupted before completion.
- Action: Started `python -m compileall -q src tests` followed by unit-test
  discovery.
- Result: The execution session was interrupted externally before it produced
  a result, so no passing baseline is claimed from this attempt.
- Next: Run compilation and unit tests as separate commands and record their
  individual results.

## 2026-08-28 - Compilation baseline

- Status: completed.
- Action: Ran `PYTHONPATH=src python -m compileall -q src tests`.
- Evidence: Command exited with code 0 and emitted no compilation errors.
- Next: Run the unit-test suite independently.

## 2026-08-28 - Unit-test baseline attempt

- Status: interrupted before completion.
- Action: Started `PYTHONPATH=src python -m unittest discover -s tests -q`.
- Result: The session was interrupted before test output or an exit code was
  returned. This does not establish a test result.
- Next: Continue with short, read-only code audit steps while preserving the
  requirement to rerun the complete suite after the next implementation.

## 2026-08-28 - Stage Artifact contract validation

- Status: completed; full-suite verification pending.
- Finding: The workflow previously accepted any successful Artifact kind from
  any stage. A misconfigured external or A2A coding Agent could return a
  `Finding` and let the workflow continue with invalid provenance.
- Change: Added an expected Artifact-kind contract for every workflow stage.
  A mismatch now records a failed `AgentContractViolation` Artifact containing
  the expected/returned kinds and the original response, then fails the task.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  wrong_artifact_kind -q` completed successfully (`Ran 1 test`, `OK`).
- Next: Run the full unit suite, then inspect the next highest-impact
  end-to-end integration gap.

## 2026-08-28 - Full-suite verification attempt after contract change

- Status: interrupted before completion.
- Action: Started `PYTHONPATH=src python -m unittest discover -s tests -q`.
- Result: The session was interrupted before the suite emitted a result or exit
  code. The targeted contract test remains the only new-test verification that
  can be claimed for this change at this point.
- Next: Keep the full-suite check pending and continue with short implementation
  and audit steps.

## 2026-08-28 - Immutable Artifact ID enforcement

- Status: completed; full-suite verification pending.
- Finding: `ArtifactStore.put_artifact` overwrote an existing JSON file when a
  later response reused its `artifact_id`, contradicting the append-only
  provenance contract.
- Change: An identical Artifact record is now an idempotent retry; a same-ID
  record with any different field raises `FileExistsError` and leaves the
  existing Artifact untouched.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  conflicting_artifact_id -q` completed successfully (`Ran 1 test`, `OK`).
- Next: Audit failure/retry paths for task-level provenance retention.

## 2026-08-28 - Retry provenance retention

- Status: completed; full-suite verification pending.
- Finding: `ResearchTask.reset_for_retry()` cleared `task.artifacts`. A later
  successful retry therefore hid previous failed Agent responses from the
  task-level provenance graph.
- Change: Retry now preserves prior Artifact IDs while resetting execution
  state. The workflow explicitly sends empty inputs to a newly restarted
  literature search and passes only that new EvidenceSet to hypothesis
  generation, preventing old failure data from influencing a new attempt.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  retry_preserves_failed_attempt -q` completed successfully (`Ran 1 test`,
  `OK`). The test executes a failed attempt followed by a successful retry.
- Next: Audit repeatable local execution and cleanup ergonomics.

## 2026-08-28 - Workflow-level cancellation boundary

- Status: completed; full-suite verification pending.
- Finding: Cancellation was evaluated by the background runner only before and
  after the whole workflow callback. A long workflow could advance from a
  completed stage into additional compute, analysis, or report stages after a
  cancellation request.
- Change: Added the terminal `CANCELLED` state and durable cancellation checks
  before and after every Agent call. Completed stage Artifacts remain stored,
  but no subsequent stage begins after cancellation is observed.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  workflow_stops_at_agent_boundary -q` completed successfully (`Ran 1 test`,
  `OK`). It requests cancellation during Coding and confirms no ExperimentRun
  or ResearchReport is created.
- Next: Check API/status handling for the new terminal state and then attempt a
  full regression run again.

## 2026-08-28 - Idempotent terminal workflow states

- Status: completed; full-suite verification pending.
- Finding: After introducing `CANCELLED`, a repeated workflow invocation could
  enter the generic error path and attempt an invalid `CANCELLED -> FAILED`
  transition.
- Change: `REPORT_READY` and `CANCELLED` are now explicit idempotent terminal
  states. Re-invoking the workflow returns the persisted task without invoking
  Agents, appending Artifacts, or rewriting state history.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  cancelled_task_is_an_idempotent -q` completed successfully (`Ran 1 test`,
  `OK`).
- Next: Cover the same behavior through the HTTP API and then retry full
  regression verification.

## 2026-08-28 - Code workspace provenance

- Status: completed; full-suite verification pending.
- Finding: CodeRevision Artifacts retained Agent stdout/stderr but did not
  establish whether the Coding Agent changed the configured working tree.
- Change: Local subprocess and Claude Code adapters now capture Git workspace
  snapshots before and after execution: HEAD, porcelain status, and hashes of
  worktree/staged diffs. `ReproducibilityPackage` exposes the latest
  CodeRevision provenance alongside experiment replay metadata.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  coding_artifact_records_git -q` completed successfully (`Ran 1 test`,
  `OK`). It initializes a temporary Git repository, makes an Agent create a
  file, and confirms the package retains the change evidence.
- Next: Run focused regression groups for workflow, storage, and API behavior;
  the complete suite remains pending because earlier full-suite commands were
  interrupted before they returned.

## 2026-08-28 - Workflow regression group

- Status: completed.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  workflow -q` completed successfully (`Ran 13 tests`, `OK`).
- Coverage: state transitions, iterative workflow paths, external Agent
  routing, contract enforcement, retry provenance, and cancellation boundaries.
- Next: Run storage and reproducibility regression tests.

## 2026-08-28 - Storage/reproducibility group filter attempt

- Status: not applicable.
- Action: Ran a combined `unittest -k` filter for storage and reproducibility.
- Result: The discovery filter matched no tests (`Ran 0 tests`, `NO TESTS
  RAN`), so this is not verification evidence.
- Next: Run the named storage and reproducibility tests separately.

## 2026-08-28 - Storage and reproducibility regression checks

- Status: completed.
- Verification:
  - `... -k reproducibility_package_checks_hashes_and_inputs -q`: `Ran 1`,
    `OK`.
  - `... -k artifact_store_rejects_conflicting_artifact_id -q`: `Ran 1`,
    `OK`.
- Coverage: package hash/input validation and immutable same-ID Artifact
  collision handling.
- Next: Run the HTTP control-plane regression group.

## 2026-08-28 - HTTP control-plane regression group

- Status: completed.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  http_control_plane -q` completed successfully (`Ran 6 tests`, `OK`).
- Coverage: task creation, approval/resume, Artifact reads, external Agent
  configuration, evidence adjudication, and asynchronous queue submission.

## 2026-08-28 - Task discovery API

- Status: completed; full-suite verification pending.
- Finding: A UI or queue client could fetch a task only after it already knew
  its ID, leaving no supported way to display persisted work as a dashboard.
- Change: Added `ArtifactStore.list_tasks()` and `GET /research`. The endpoint
  returns compact task summaries, including task state, execution status,
  Artifact count, iteration progress, and current error. It skips malformed
  task files rather than hiding all recoverable work.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  http_control_plane_lists_task_summaries -q` completed successfully (`Ran 1
  test`, `OK`), including the empty-store case.
- Next: Run a complete unit-test suite and a CLI smoke run, then record the
  final evidence for this development pass.

## 2026-08-28 - Full-suite verification attempt after task discovery

- Status: interrupted before completion.
- Action: Started `PYTHONPATH=src python -m unittest discover -s tests -q`.
- Result: The session was interrupted before it returned output or an exit code.
  This is not a passing-suite result. Focused workflow, API, storage, and new
  feature tests remain recorded separately above.
- Next: Run short CLI and HTTP smoke checks, then continue implementation from
  the remaining end-to-end gaps.

## 2026-08-28 - CLI and HTTP smoke checks

- Status: completed.
- Verification:
  - `python -m autoresearch.cli "progress smoke" --store .progress-smoke`
    reached `REPORT_READY` with 9 Artifacts and `error: null`.
  - A short in-process `ResearchApiServer` check returned `{"task_count": 0}`
    from `GET /research` for a new empty store.
- Next: Continue auditing external/A2A error provenance.

## 2026-08-28 - Remote failure provenance retention

- Status: completed; full-suite verification pending.
- Finding: A remote A2A failure produced `A2ATaskFailure`, but stage-kind
  validation replaced it with an `AgentContractViolation` before the original
  peer response reached persistent Artifact storage.
- Change: On a kind mismatch, the workflow now persists the original returned
  Artifact first, then stores a failed `AgentContractViolation` whose inputs
  include the returned Artifact ID. Both peer diagnostics and local contract
  context remain auditable.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  remote_failure_before_contract -q` completed successfully (`Ran 1 test`,
  `OK`).
- Next: Audit whether retry and reproducibility package logic retains these
  failed external Artifacts correctly.

## 2026-08-28 - Latest-attempt reproducibility summaries

- Status: completed; full-suite verification pending.
- Finding: After preserving failed attempts for provenance, the package chose
  the first `EvidenceSet` and `ReviewReport` from task history. A successful
  retry could therefore expose stale failed-attempt summaries.
- Change: The package manifest continues to include all Artifacts, while its
  evidence and review summary fields now select the latest matching Artifact.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  reproducibility_package_uses_latest_attempt -q` completed successfully
  (`Ran 1 test`, `OK`).
- Next: Audit report generation for the same latest-attempt selection rule.

## 2026-08-28 - Task-list filtering test failure

- Status: fixed; re-verification pending.
- Action: Added `GET /research` filtering and ran its directed test.
- Result: The test exposed a missing `ResearchState` import in `api.py`; a
  state-filtered request raised `NameError` and closed the HTTP connection.
- Fix: Imported `ResearchState` in the API module. The same test will be
  rerun before recording this feature as complete.

## 2026-08-28 - Task-list state and execution filters

- Status: completed; full API and suite verification pending.
- Change: `GET /research` accepts repeatable `state` and `execution_status`
  filters. Unknown query fields, empty execution statuses, and invalid states
  return `400 invalid_request` rather than being silently ignored.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  http_control_plane_filters_task_summaries -q` completed successfully after
  fixing the missing `ResearchState` import (`Ran 1 test`, `OK`).
- Next: Run the HTTP control-plane regression group and continue the remaining
  end-to-end audit.

## 2026-08-28 - Consolidated research status endpoint

- Status: completed; full-suite verification pending.
- Finding: A dashboard had to join task state, Artifact IDs, and latest
  evidence/Finding/review/report records itself.
- Change: Added `research_status()` and `GET /research/{task_id}/status`, a
  read-only consolidated summary that retains source Artifact IDs and exposes
  iteration objective, cancellation state, latest failure, and reproducibility
  package reference.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  consolidated_research_status -q` completed successfully (`Ran 1 test`,
  `OK`).
- Next: Run the full HTTP regression group after this endpoint addition, then
  attempt complete suite verification again.

## 2026-08-28 - Explicit orphaned-task retry

- Status: completed; full-suite verification pending.
- Finding: The queue correctly marked work as `orphaned` after process restart,
  but the API exposed no controlled way to resubmit it with a replacement
  execution profile.
- Change: Added `POST /research/{task_id}/retry`. It accepts only `orphaned`
  or `FAILED` tasks, preserves all prior Artifacts, resets execution state, and
  submits a new bounded local job. Omitted iteration/objective/replicate fields
  inherit persisted task values; external commands must still be supplied
  explicitly because they are never persisted.
- Verification: `python -m unittest discover -s tests -p test_mvp.py -k
  http_retry_resubmits_orphaned -q` completed successfully (`Ran 1 test`,
  `OK`), including simulated control-plane restart and successful requeue.
- Next: Run queue/API regression groups and inspect the next protocol gap.

## 2026-08-28 - Combined regression filter attempt

- Status: not applicable.
- Action: Tried one `unittest -k` expression intended to select both queue and
  HTTP tests.
- Result: The expression matched no test methods (`Ran 0 tests`, `NO TESTS
  RAN`), so it is not verification evidence.
- Next: Run the queue and HTTP groups as separate exact filters.

## 2026-08-28 - Queue regression group attempt

- Status: interrupted before completion.
- Action: Started the queue-focused `unittest` discovery command.
- Result: The session was interrupted before output or an exit code was
  returned; no pass/fail claim is made.
- Next: Execute queue test methods directly by name to avoid discovery-session
  interruptions.

## 2026-08-28 - Queue retry identity

- Status: completed; remaining queue regression tests pending.
- Verification: `$env:PYTHONPATH='src'; python -m unittest discover -s tests
  -p test_mvp.py -k test_background_runner_retries_failed_task_with_same_id
  -q` completed successfully (`Ran 1 test`, `OK`).
- Result: A failed background task can be retried while retaining its original
  task identity and persisted provenance.
- Next: Verify orphaned-job recovery and live-lock protection.

## 2026-08-28 - Orphaned-job recovery

- Status: completed; remaining queue regression tests pending.
- Verification: `$env:PYTHONPATH='src'; python -m unittest discover -s tests
  -p test_mvp.py -k test_background_runner_recovers_orphaned_job_and_lock
  -q` completed successfully (`Ran 1 test`, `OK`).
- Result: A stale persisted job and its stale lock are marked `orphaned` for
  explicit operator-controlled retry rather than silently re-executed.
- Next: Verify that a live lock owner prevents false orphan recovery.

## 2026-08-28 - Live-lock protection attempt

- Status: interrupted before completion.
- Action: Started the directed live-lock test with the unittest discovery
  method filter.
- Result: The tool session was externally interrupted before it returned test
  output or an exit code. This is not recorded as a passed or failed test.
- Next: Re-run the same method through a direct test module invocation with a
  shorter process lifetime.

## 2026-08-28 - Direct live-lock test attempt

- Status: interrupted before completion.
- Action: Invoked `tests.test_mvp.MvpTests.test_background_runner_does_not_recover_job_with_live_lock_owner`
  directly via `python -m unittest`.
- Result: The desktop execution session was again interrupted before any test
  result or exit code was returned. No verification claim is made.
- Next: Inspect the test and queue implementation statically, then use a
  bounded, non-blocking equivalent check if it covers the same invariant.

## 2026-08-28 - Windows live-lock probe defect

- Status: fixed; directed verification pending.
- Finding: `_process_alive()` used `os.kill(pid, 0)`. Unlike POSIX, Windows
  does not provide safe signal-zero probe semantics through this API; probing
  the test process's own PID could terminate it. This explained the repeated
  output-free desktop-session interruptions in the live-lock test.
- Change: On Windows, `queue.py` now calls `OpenProcess` with
  `PROCESS_QUERY_LIMITED_INFORMATION` and checks `GetExitCodeProcess` against
  `STILL_ACTIVE`. POSIX retains the existing signal-zero probe.
- Next: Re-run the exact live-lock test, then the two cancellation tests.

## 2026-08-28 - Windows live-lock protection verification

- Status: completed; cancellation regression tests pending.
- Verification: `$env:PYTHONPATH='src'; python -m unittest discover -s tests
  -p test_mvp.py -k test_background_runner_does_not_recover_job_with_live_lock_owner
  -q` completed successfully (`Ran 1 test`, `OK`).
- Result: A lock owned by the live current process remains in place and its
  running job is not marked `orphaned`.
- Next: Verify cancellation persistence and stale-success protection.

## 2026-08-28 - Queue cancellation verification

- Status: completed; compile and broader regression checks pending.
- Verification: Both directed tests completed successfully (`Ran 1 test`,
  `OK` each):
  - `test_background_runner_cancellation_is_recorded`
  - `test_background_runner_does_not_mark_stale_success_after_cancellation`
- Result: In-flight cancellation is persisted as `cancelled`, and a callback
  returning a stale `REPORT_READY` object cannot revive the task or job.
- Next: Run compileall and a bounded broader regression selection.

## 2026-08-28 - Compile verification

- Status: completed; broader regression verification pending.
- Verification: `python -m compileall -q src tests` completed successfully
  with no syntax or bytecode compilation errors.
- Next: Run the focused workflow and HTTP regression groups, then attempt the
  complete test suite once more.

## 2026-08-28 - Focused workflow and HTTP regression

- Status: completed; complete suite pending.
- Verification: Workflow selection completed successfully (`Ran 14 tests in
  2.010s`, `OK`); HTTP selection completed successfully (`Ran 11 tests in
  6.788s`, `OK`).
- Result: Core workflow transitions and the HTTP control-plane behavior pass
  together after the queue probe fix.
- Next: Attempt the complete `tests` suite.

## 2026-08-28 - Complete regression suite

- Status: completed.
- Verification: `$env:PYTHONPATH='src'; python -m unittest discover -s tests
  -q` completed successfully (`Ran 71 tests in 11.569s`, `OK`).
- Result: The full current test suite passes, including workflow, A2A,
  provenance, HTTP control plane, queue recovery, and cancellation behavior.
- Next: Perform final source/documentation/status audit and report the shipped
  state without overstating deployment readiness.

## 2026-08-28 - Windows process-query hardening

- Status: implemented; verification pending.
- Change: Added explicit `ctypes`/`wintypes` signatures for Windows
  `OpenProcess`, `GetExitCodeProcess`, and `CloseHandle`, preventing 64-bit
  handle truncation. Access-denied process queries are treated as live so an
  uninspectable lock is never taken over automatically.
- Next: Re-run the live-lock test, compile check, and complete suite.

## 2026-08-28 - Hardening verification

- Status: completed; final suite pending.
- Verification: Live-lock directed test completed successfully (`Ran 1 test`,
  `OK`); `python -m compileall -q src tests` completed with no errors.
- Next: Re-run the complete test suite after the Windows API signature change.

## 2026-08-28 - Final audit

- Status: completed.
- Verification: Complete suite after hardening completed successfully (`Ran 71
  tests in 11.572s`, `OK`); compileall and the live-lock directed test also
  remain green. README documents the current API, A2A, provenance, retry,
  cancellation, and deployment limitations. `.gitignore` excludes generated
  stores, smoke-check directories, and Python caches; no Git metadata is
  present in this workspace.
- Result: The current MVP implementation and its documented local-development
  boundaries are consistent with the verified source state.
- Next: For production use, add authenticated HTTPS transport, durable shared
  queue/storage, and domain-specific scientific evaluation suites.

## 2026-08-28 - Architecture walkthrough

- Status: completed; no source-code change.
- Action: Read and reconciled the current workflow, agent adapters, A2A
  transport, storage/queue, API, and CLI wiring in order to explain the
  implementation and actual provider choices.
- Result: The project uses an artifact-first controller with replaceable local,
  subprocess, Claude Code, and remote A2A stage adapters; DeepResearch and
  DeerFlow are literature-source adapters rather than embedded model clients.

## 2026-08-28 - DeerFlow integration attempt

- Status: blocked by incomplete third-party source snapshot; AutoResearch source
  remains unchanged.
- Completed: Located `third_party/deer-flow-main`, verified the official
  DeerFlow 2.1.0 backend layout, created local `config.yaml` and
  `extensions_config.json` from the upstream examples, installed its backend
  lockfile environment (`225` packages), and verified `deerflow --help`.
- Failure: Headless runtime import fails because
  `backend/packages/harness/deerflow/skills/skillscan/__init__.py` imports
  `orchestrator.py`, but that file is absent from the supplied snapshot.
  Attempts to fetch the official raw file were blocked by the local Windows
  file security layer; an official GitHub shallow clone also failed because
  the connection was reset. No credentials were entered or persisted.
- Cleanup: Reverted temporary third-party import/probe changes. The existing
  AutoResearch `DeerFlowSource` adapter still targets the documented
  `deerflow --json` contract.
- Next: Supply a complete DeerFlow checkout (Git clone or a fresh archive that
  contains `backend/packages/harness/deerflow/skills/skillscan/orchestrator.py`)
  or manually copy that file into the extracted tree; then rerun the import and
  headless smoke checks.

## 2026-08-28 - DeerFlow file restored

- Status: completed; model-backed smoke test pending.
- Action: After the Windows isolation was lifted, restored the missing upstream
  `skillscan/orchestrator.py` into the extracted checkout.
- Verification: The file is readable (`61,198` bytes), `from deerflow.skills.skillscan
  import scan_skill_dir` imports successfully, and `uv run --locked --project
  backend deerflow --help` succeeds.
- Next: Run one bounded headless invocation; any remaining failure should now
  identify configuration or provider credentials rather than a missing module.

## 2026-08-28 - DeerFlow model configuration

- Status: configuration prepared; provider credential pending.
- Change: Activated a minimal OpenAI Responses API model entry in DeerFlow's
  local `config.yaml`, referencing only the environment variable
  `$OPENAI_API_KEY`.
- Verification: DeerFlow configuration loading now reaches environment-variable
  resolution, but the current shell has no `OPENAI_API_KEY`; it fails closed
  with `Environment variable OPENAI_API_KEY not found`. No key was read,
  displayed, or written.
- Next: Set `OPENAI_API_KEY` in the shell that launches DeerFlow, then rerun the
  bounded headless smoke test. A Codex CLI login by itself is not automatically
  an OpenAI API credential for DeerFlow.

## 2026-08-28 - OpenAI-compatible relay configuration

- Status: configured; safe credential setup pending.
- Finding: `https://sorryios.ai/v1/models` responds as the OpenAI-compatible
  API surface; the user-provided `/codex` URL is not the model API base path.
- Change: DeerFlow's local model entry now uses `base_url: https://sorryios.ai/v1`.
  The API key remains an environment-variable reference only.
- Security: A key was pasted into chat and must be revoked/rotated; it was not
  persisted or used by the tooling.
- Next: User sets a newly generated key locally as `OPENAI_API_KEY`, then the
  model list and one-sentence DeerFlow smoke test can be run.

## 2026-08-28 - Exposed credential handling

- Status: awaiting safe credential rotation.
- Action: Declined to use the API key pasted into chat because it is exposed
  and must not be persisted, echoed, or passed through tool commands.
- Next: User should revoke/rotate that key and set the replacement locally via
  `OPENAI_API_KEY`; no secret needs to be sent in chat.

## 2026-08-28 - Credential environment visibility

- Status: awaiting environment handoff.
- Verification: The current Codex tool process reports
  `OPENAI_API_KEY_PRESENT=False`; it cannot see the variable set in a separate
  user PowerShell session. The value was not requested, displayed, or stored.
- Next: Set the replacement key in the same process that launches DeerFlow, or
  restart the terminal/Codex process so the environment is inherited, then run
  the model-list and headless smoke checks.

## 2026-08-28 - Codex CLI connectivity

- Status: completed.
- Verification: The user's Windows user-level key was read into a process-only
  `CODEX_API_KEY` mapping (value never printed or persisted). `codex exec
  --ephemeral --skip-git-repo-check --sandbox read-only` returned the exact
  requested `OK` with exit code 0.
- Finding: Codex CLI diagnostics identify the relay as a Codex-specific
  endpoint (`chat1.sorryios.io/codex/...`), so it should be used through the
  Codex CLI rather than DeerFlow's standard OpenAI `/v1` model adapter.
- Next: Add and verify the AutoResearch Codex Coding Agent adapter.

## 2026-08-28 - Codex Coding Agent adapter

- Status: completed.
- Change: Added `CodexCodingAgent` in `src/autoresearch/coding.py`; it invokes
  `codex exec --json --sandbox workspace-write`, passes bounded Artifact context
  in the prompt, captures JSONL stdout/stderr, and records Git workspace
  provenance. Credentials remain process-environment-only.
- Change: Added `--coding-agent codex` and `--codex-executable` to the CLI,
  execution-profile hashing/resume flow, and HTTP API profile validation and
  dispatch.
- Verification: `python -m compileall -q src tests` passed; Codex selection
  instantiated as `CodexCodingAgent`; the existing full suite passed (`Ran 71
  tests`, `OK`).
- Next: Run one real Codex Coding Agent invocation in a disposable workspace,
  then wire the successful configuration into the first research task.

## 2026-08-28 - Real Codex adapter smoke test

- Status: completed.
- Verification: With the user-level key mapped only into the child process's
  `CODEX_API_KEY`, `CodexCodingAgent` ran in the ignored `.tmp-autoresearch`
  workspace and returned `CodeRevision` with `status=created`,
  `provider=codex-cli`, captured stdout, and `workspace_change_detected=False`.
  The prompt requested no file changes; no key appeared in the Artifact.
- Result: AutoResearch can now invoke the user's Codex relay through the
  Coding stage without Claude Code.
- Next: Use `--coding-agent codex --coding-cwd <repo>` in the first real task;
  keep DeerFlow model configuration separate until its relay endpoint is
  confirmed compatible.

## 2026-08-28 - Codex documentation and CLI verification

- Status: completed.
- Change: Documented Codex CLI setup and credential boundaries in `README.md`.
- Verification: `python -m compileall -q src tests` passed; CLI help exposes
  both `--coding-agent ... codex` and `--codex-executable`.

## 2026-08-28 - Third-party workspace

- Status: completed.
- Action: Created the external-project staging directory
  `F:\AutoDeepResearch\third_party`.
- Result: DeerFlow can now be extracted under
  `F:\AutoDeepResearch\third_party\deerflow` without mixing its source with
  the AutoResearch package.
- Next: Inspect DeerFlow after extraction and determine its supported
  headless/API invocation.

## 2026-08-28 - DeerFlow source inspection

- Status: completed; runtime installation pending.
- Finding: The extracted directory is `third_party/deer-flow-main` and contains
  the complete DeerFlow 2.1.0 monorepo. Its backend requires Python 3.12+ and
  exposes the `deerflow` CLI through the workspace harness; documented modes
  include `deerflow --print` and newline-delimited `deerflow --json`.
- Integration note: The existing `DeerFlowSource` adapter already targets the
  documented `--json` stream and extracts only explicit citation links, while
  retaining the raw event stream as a source snapshot.
- License: The repository includes its LICENSE file; source will remain under
  `third_party` and will not be copied into `src`.
- Next: Install DeerFlow's backend lockfile environment and run a CLI health
  check before wiring an actual model key.
# 2026-08-28 - DeerFlow relay base URL correction

- Action: Corrected DeerFlow's OpenAI-compatible model base URL to the same
  Codex relay base path used by the local Codex CLI (`https://chat1.sorryios.io/codex`),
  removing the previously assumed `/v1` suffix.
- Credential: Kept API key environment-only (`$OPENAI_API_KEY`); no secret was
  written to configuration or logs.
- Next: Run a headless DeerFlow generation smoke test to verify the relay's
  Responses API compatibility.

## 2026-08-28 - DeerFlow relay path and model probe

- Verification: With the `/v1` suffix removed, DeerFlow reached the relay's
  Responses endpoint (the error changed from an unavailable route to a model
  validation response).
- Finding: The relay rejected `gpt-5` for a ChatGPT Codex account. Updated the
  configured model to `gpt-5.6-terra`, matching the working local Codex CLI.
- Next: Re-run the headless smoke test; if the relay rejects this model too,
  the remaining blocker is the relay's model allow-list rather than URL format.

## 2026-08-28 - DeerFlow relay smoke result

- Verification: Headless DeerFlow reached `https://chat1.sorryios.io/codex`
  successfully with model `gpt-5.6-terra`.
- Finding: The relay accepted the route/model but returned `System messages are
  not allowed`; this is now a message-format incompatibility, not a URL or
  authentication failure.
- Next: Add a DeerFlow model shim that converts its system instruction into the
  relay-supported instruction format, then repeat the smoke test.

## 2026-08-28 - DeerFlow Codex message compatibility shim

- Change: Added `CodexRelayChatOpenAI`, preserving DeerFlow's system-instruction
  text while encoding unsupported `system` roles as high-priority user context
  in both Responses (`input`) and Chat Completions (`messages`) payloads.
- Config: DeerFlow now uses this shim with the Codex relay base URL (without
  `/v1`) and model `gpt-5.6-terra`.
- Next: Verify a real headless response and then wire the adapter into the
  AutoResearch literature stage.

## 2026-08-28 - DeerFlow connection completed

- Verification: `uv run --locked --project backend deerflow --print 'Reply with
  exactly DEERFLOW_OK'` returned `DEERFLOW_OK` through the Codex relay.
- Result: DeerFlow model invocation is operational with the local environment
  key, relay base URL without `/v1`, model `gpt-5.6-terra`, and the new message
  compatibility shim. No credential was written to disk or output.
- Remaining: Run a full AutoResearch task to validate citation/event parsing;
  the standalone DeerFlow generation path is now connected.

## 2026-08-28 - First end-to-end digit recognition task

- Task: Compared logistic regression baseline with RBF-SVM candidate on the
  built-in sklearn handwritten-digits dataset.
- Verification: AutoResearch completed `REPORT_READY` with two candidate
  replicates and one baseline. Baseline accuracy was 0.9622; candidate mean
  accuracy was 0.9911 (descriptive only).
- Store: `.autoresearch-digits3`; report Artifact id
  `b94e7568-76cd-4a80-8d0c-0ecae8e497e3`.

## 2026-08-28 - Human-readable report output

- Change: CLI now writes the latest `ResearchReport` to `<store>/report.md` in
  addition to immutable JSON Artifacts.
- Applied: Materialized `.autoresearch-digits3/report.md` for the completed
  digit-recognition task.

## 2026-08-28 - Agent architecture documentation

- Action: Added `AGENT_ARCHITECTURE.md`, documenting the end-to-end state
  machine, every current Agent/module, DeerFlow and Codex boundaries, A2A
  status, operational limits, and the proposed Literature Intelligence flow.

## 2026-08-28 - Adaptive research feedback loop

- Change: Added `ResearchCriticAgent` and `ResearchDecision` Artifact. After
  every analysis it evaluates baseline deltas and emits a bounded decision;
  optional remote routing uses `--a2a-agent critic=URL`.

## 2026-08-28 - Handwritten digit demo prepared

- Action: Added a deterministic `sklearn` digits task under
  `examples/digit_recognition` with logistic-regression baseline and RBF-SVM
  candidate. Both emit `{"metrics":{"accuracy":...}}` for ComputeAgent.
- Dataset: Built-in sklearn digits (no external download).
- First run exposed an sklearn 1.9 API change (`multi_class` removed from
  `LogisticRegression`); removed that obsolete argument before retrying.
- Second run exposed strict JSON parsing: Python dict output is not valid JSON;
  both experiment scripts now emit `json.dumps(...)` output.

## 2026-08-28 - Adaptive loop completion and verification

- Change: On a non-improving result, the Critic now triggers a new
  `HypothesisSet` using the EvidenceSet, Finding and ResearchDecision, followed
  by a refreshed claim-evidence map before the next iteration.
- Change: The latest ResearchDecision now flows into Reviewer, ResearchReport,
  and human-readable `report.md`.
- Verification: `python -m unittest discover -s tests -q` passed (71 tests).
  Two-iteration digits runs reached `REPORT_READY`, including a no-improvement
  run that emitted `revise_hypothesis` and created a replacement HypothesisSet.

## 2026-08-28 - PC / 普通服务器资源范围

- Decision: The project targets personal PCs and ordinary single-server
  environments. Large-scale cluster scheduling, multi-node training and
  Kubernetes/Slurm integration are explicitly out of scope.
- Documentation: Updated `AGENT_ARCHITECTURE.md` with resource-aware research
  principles: bounded experiments, small proxy runs, timeouts, budgets and
  resumability.

## 2026-08-28 - Writer deployment boundary clarified

- Correction: The external architecture explicitly includes a fifth
  Writer/Reviewer role. `ReportWriterAgent` already exists for structured,
  evidence-linked reports; it can be co-located with Reviewer on a PC, while a
  future `PaperWriter` layer will handle complete submission-style manuscripts.

## 2026-08-28 - Roadmap reprioritized

- Decision: Reprioritized remaining work for PC/ordinary-server use: literature
  intelligence, research design, adaptive hypothesis branching, rigorous
  single-machine experimentation, paper/review output, then optional secured
  A2A deployment. Large-scale scheduling remains out of scope.

## 2026-08-28 - Logic consistency check

## 2026-08-28 - Adaptive-loop regression coverage

- Test: Added regression coverage for per-iteration `ResearchDecision` output,
  final report feedback propagation, and non-improving baseline results that
  regenerate a second `HypothesisSet`.
- Verification: Full suite now passes 72 tests.

## 2026-08-28 - Critic early-stop control flow

- Change: The workflow now honors a `ResearchDecision.decision=stop_early`
  response from a Critic (including an A2A Critic), ending the iteration loop
  and moving directly to review.
- Verification: Added an early-stop regression test; compile succeeds and the
  full suite passes 73 tests.

## 2026-08-28 - DeerFlow literature intelligence implementation

- Change: Extended `DeerFlowSource` with a strict tagged JSON brief contract.
  It now requests and parses `paper_cards`, `comparison_matrix`,
  `gap_candidates`, and `research_plan`, while retaining citation extraction as
  a fallback and labelling synthesis as candidate evidence.
- Change: Added `literature_intelligence` to `EvidenceSet`, including source
  levels and conservative gap-candidate fields. No synthesis is marked as a
  verified scientific conclusion.
- Fix: DeerFlow child processes now force UTF-8 output on Windows to prevent
  GBK encoding failures on Chinese research prompts.
- Verification: Real DeerFlow `--json` call succeeded through the Codex relay;
  returned 2 paper cards and 2 gap candidates. Full test suite passes 74 tests.

## 2026-08-28 - On-demand full-text policy

## 2026-08-28 - On-demand full-text evidence fields

- Change: DeerFlow's structured brief now asks it to open full text only for
  the top three lawful, accessible papers and return short excerpts with
  section/page locators; bulk PDF downloading remains disabled.
- Change: Excerpts are normalized into `EvidenceSet.full_text_passages` with
  SHA-256, source URL, and `candidate_unverified` status for downstream claim
  mapping.
- Fix: DeerFlow subprocesses force UTF-8 on Windows for Chinese JSON output.
- Verification: Compile and unit suite pass (74 tests).

## 2026-08-28 - Full-text evidence integration test

- Test: Added coverage proving structured DeerFlow `full_text_evidence` is
  normalized into `EvidenceSet.full_text_passages` with locator and
  `candidate_unverified` status.
- Verification: Full test suite passes 75 tests.

## 2026-08-28 - 80% milestone Phase A deliverables

- Change: Structured DeerFlow intelligence is now persisted as a dedicated
  `LiteratureIntelligence` Artifact with flattened paper cards, comparison
  rows, gap candidates and research plans.
- Change: CLI materializes human-readable `paper_cards.md`,
  `comparison_matrix.md`, `innovation_brief.md`, and `research_plan.md` next
  to `report.md` when intelligence is available.
- Verification: Added end-to-end persistence/rendering regression coverage;
  compile succeeds and the full suite passes 76 tests.

## 2026-08-28 - Research design handoff

- Change: Non-empty DeerFlow research plans are now promoted to a dedicated
  `ResearchPlan` Artifact with a PC/ordinary-server resource policy. This makes
  the handoff from literature intelligence to hypothesis and coding explicit
  and provenance-linked.

## 2026-08-28 - Research plan context propagation

- Change: The workflow now passes `ResearchPlan`, latest `HypothesisSet`,
  `ResearchDecision`, and `Finding` explicitly into each Coding iteration;
  initial Hypothesis generation also receives the literature intelligence and
  plan Artifacts. This makes the literature-to-experiment handoff executable,
  rather than documentation-only.
- Verification: Compile succeeds and the full suite passes 76 tests.

## 2026-08-28 - Nature skill rules integrated into Writer/Reviewer

- Change: Report artifacts now carry evidence-first writing, Nature-style
  reviewer axes, and transparent statistics profiles. The local Writer emits a
  complete manuscript skeleton with explicit `AUTHOR_INPUT_NEEDED` placeholders
  and conservative claim rules.
- Boundary: These are locally enforced output contracts inspired by the named
  skills; no hidden external skill service or unsupported publication claim is
  made. A configured external Writer/Reviewer A2A adapter can replace the local
  implementation while retaining the same Artifact contract.
- Verification: Compile succeeds and the full suite passes 76 tests.
- Applied: Regenerated `.autoresearch-critic-verified/manuscript.md` to verify
  the readable manuscript sidecar is materialized from a completed report.

## 2026-08-28 - Single-machine statistical guardrails

- Change: Analysis Artifacts now include sample size, standard error, a clearly
  labelled descriptive 95% confidence interval (when `n > 1`) and baseline
  delta/effect-size fields. The report continues to mark results descriptive;
  no inferential significance is claimed.

## 2026-08-28 - 80% milestone defined

- Decision: Prioritize a PC/ordinary-server usable research assistant rather
  than cluster scheduling. The 80% milestone requires an evidence-bound
  literature chain, explicit research design, rigorous bounded local
  experiments, adaptive feedback, complete paper structure, two independent
  reviewers, and one-command reproducibility.
- Roadmap: Ordered implementation as Paper Reader/Evidence binding → Research
  Planner/gap falsification → local statistics and branch experiments → Paper
  Writer/multi-reviewer revision.

- Decision: Literature Intelligence defaults to opening full text on demand
  after shortlist filtering, not bulk PDF downloading. It keeps extracted
  passages, locators and hashes; offline PDF archiving is opt-in for specified
  papers and lawful access routes only.

- Corrected note: hypothesis adapters advertise `revise_hypotheses`, matching
  the adaptive workflow action; compile and all 71 tests pass.

- Verification: Updated hypothesis adapters advertise evise_hypotheses, matching the adaptive workflow action; compile and all 71 tests pass.
- Verification: Added regression assertions for descriptive 95% confidence
  interval fields; full suite remains green at 76 tests.

## 2026-08-28 - Research plan consumption and manuscript handoff

- Change: Fixture Hypothesis and Coding agents now consume the normalized
  `ResearchPlan` fields (baseline, candidate, metric, failure condition and
  resource budget) and persist the consumed subset in their Artifacts.
- Change: `ResearchReport` and `manuscript.md` now expose the exact plan used by
  downstream stages, preserving an auditable literature-to-experiment handoff.
- Verification: Compile and full unit suite pass (`Ran 77 tests`, `OK`).

## 2026-08-28 - Local multi-lens review synthesis

- Change: The default local Reviewer now emits three independent review lenses
  (methodology, evidence, significance) over the immutable Artifact set and a
  control-plane synthesis, while preserving the existing `ReviewReport`
  contract and human-review gate.
- Boundary: These are isolated local review contexts, not claims of accepted
  peer review; external A2A Reviewer services can still replace the adapter.
- Verification: Full unit suite passes (`Ran 77 tests`, `OK`).

## 2026-08-28 - 80% acceptance smoke test

- Verification: CLI fixture workflow with two iterations and two replicates
  reached `REPORT_READY` with a reproducibility Artifact, `report.md` and
  `manuscript.md`; full unit suite remains green at 77 tests.

## 2026-08-28 - Experiment role simplification

- Decision: Expose one `Codex Experiment` role to users. Coding and execution
  remain separate internally only for reproducibility, budgets and auditability.
- Clarification: `LocalComputeAgent`/`DockerComputeAgent` are runners, not
  additional LLM Agents or remote services; the Orchestrator invokes them after
  Coding produces the experiment code.
- Documentation: Updated `AGENT_ARCHITECTURE.md` and `README.md` to reflect
  the four-role deployment boundary.

## 2026-08-28 - Local product console

- Change: Added a polished dependency-free single-page frontend under
  `frontend/` with a monochrome/acid-green scientific console visual system.
- Features: mission creation, literature mode and budget controls, task list,
  stage pipeline, Artifact stream, payload inspection and API endpoint switch.
- Change: Added development CORS headers to the loopback API so the static UI
  can run on port 5173 without a frontend build server.
- Verification: Browser DOM/screenshot smoke test loaded the page and showed
  the empty dashboard; API POST smoke test returned HTTP 201 and the static
  assets returned HTTP 200 after the frontend field contract was corrected.

## 2026-08-28 - Theme switch

- Change: Added a top-right accessible theme switch (`role=switch`) for the
  monochrome console. The light theme remaps panels, borders, text and hover
  states while retaining the acid-green signal color.
- Change: Theme selection persists in browser local storage and updates the
  switch label/ARIA state for keyboard and assistive-technology users.
- Verification: JavaScript syntax check passes and the static asset endpoint
  returns the updated theme control bundle.

## 2026-08-28 - Dark theme default compatibility

- Fix: Versioned the theme preference key so an old prototype's light-mode
  value cannot remove the original dark background after the UI upgrade.
- Behavior: Fresh/legacy sessions now open dark; the new switch can still opt
  into light mode and persists that choice under the versioned key.

## 2026-08-28 - Full canvas theme coverage

- Fix: Restored explicit `html/body` theme backgrounds and applied the active
  background token to `.app-shell`, `.main` and `.content`; the previous
  switch only recolored selected panels because the global body rule was
  missing from the compressed stylesheet.
- Verification: Browser DOM confirmed the accessible switch changes state;
  visual smoke screenshot confirmed the entire main canvas, sidebar and card
  surfaces change to the light theme, then the default dark theme was restored.

## 2026-08-28 - Live mission progress visibility

- Change: Mission detail view now displays execution status, iteration progress,
  latest Artifact kind and background job status in dedicated telemetry cards.
- Change: The UI polls task, status, Artifact and job endpoints together; the
  `created` Artifact status is now clearly separated from workflow progress.
- Verification: Static JavaScript syntax check passes; the existing browser
  smoke flow showed the task pipeline and Artifact stream, and the new status
  endpoint contract is consumed without changing backend semantics.

## 2026-08-29 - Real runtime configuration in console

- Change: Added an expandable `REAL RUNTIME CONFIG` panel to the frontend.
  Users can choose Demo/Fake, Codex CLI, Claude Code CLI or a custom coding
  adapter, and configure code workspace, experiment command/workspace and CLI
  executable names.
- Change: Console now maps its form values to the API's real contract,
  including splitting experiment/custom commands into argument arrays and
  passing `literature`, `coding_agent`, `coding_cwd`, `compute_command`,
  `compute_cwd` and DeerFlow workspace fields.
- Safety: API keys are never entered or persisted by the UI; runtime
  authentication remains in the local process environment. Configuration is
  only persisted in browser local storage for convenience.
- Verification: `node --check frontend/app.js` passes; configuration field and
  payload wiring were audited against `_profile_args` accepted API keys.

## 2026-08-29 - Demo report transparency and pipeline labels

- Change: Research reports now carry `execution_mode`; reports generated from
  synthetic FakeCompute output are visibly labelled `DEMO DATA` in the UI.
- Fix: Pipeline stage labels are wrapped in dedicated spans with no-wrap
  styling, preventing the final `REPORT_READY` label from rendering as the
  concatenated `DOCREPOR` string.
- Verification: `node --check frontend/app.js` and the full 77-test suite pass.

## 2026-08-29 - Live activity indicator

- Change: Added a live activity banner to each mission detail page. It maps
  workflow states to human-readable actions such as “正在检索文献”、“Coding Agent
  正在实现实验” and “实验执行器正在运行实验”.
- Change: Running missions show an animated spinner and pulse marker; terminal
  states stop the animation and retain the final action text. The mission list
  also shows a mini spinner for non-terminal tasks.
- Verification: Static assets return successfully, `node --check frontend/app.js`
  passes, and the complete Python suite remains green (`Ran 77 tests`, `OK`).

## 2026-08-29 - Workflow trace and report trajectory

- Change: Mission details now include an expandable `TRACE` timeline built from
  the persisted task history. Every transition shows `from → to`, timestamp
  and the corresponding human-readable activity, while the live banner remains
  the current step indicator.
- Change: The report reader now includes an iteration/replicate trajectory
  table with per-run metrics and a clear synthetic-demo explanation when Fake
  output is detected. This makes the generic summary auditable instead of
  presenting it as a complete scientific result.
- Verification: Static assets expose the trace and trajectory code; `node
  --check frontend/app.js` and all 77 Python tests pass.

## 2026-08-29 - Task-specific report transparency

- Finding: Different questions still showed the same `score=0.81` and generic
  Finding because the frontend default was Demo/Fake and the local fixture
  agents intentionally return synthetic values.
- Change: Report summaries now include the actual research question and
  execution mode. The report reader shows the question-specific hypothesis,
  marks `SYNTHETIC DEMO` explicitly, and retains the per-run trajectory table.
- Boundary: Existing completed Artifacts are immutable; rerun a task after
  selecting a real Coding Agent and experiment command to obtain real metrics.
- Verification: Static asset loading and JavaScript syntax pass; all 77 Python
  tests remain green.

## 2026-08-29 - Existing-code execution and metric selection

- Finding: A Coding Artifact listing `experiment.py` does not itself execute
  that file; without an experiment command the workflow correctly fell back to
  FakeCompute. The digits scripts also emit `accuracy`, while the UI had
  hard-coded the objective metric `score`.
- Change: When an explicit Coding workspace is configured and no compute
  command is supplied, the backend conservatively auto-discovers an existing
  `experiment.py`, `candidate.py` or `run_experiment.py` and wires it to the
  local runner. No arbitrary filenames are executed.
- Change: The frontend now exposes an Objective Metric field and sends it to
  the API. Existing digit code was verified through the real local runner with
  `accuracy=0.9911111111` and `execution_mode=real_configured`.
- Verification: Full suite passes (`Ran 79 tests`, `OK`); CLI real-code smoke
  reaches `REPORT_READY` with a non-synthetic ExperimentRun.

## 2026-08-29 - Local console restart helper

- Fix: Restarted the stopped loopback API and static UI services; ports 8090 and
  5173 now return HTTP 200.
- Change: Added `start_console.ps1` to start both services as hidden local
  processes with one command, avoiding the common “127.0.0.1 refused to
  connect” state after a terminal session closes.

## 2026-08-28 - Report-ready entry point

- Change: Completed missions now show an explicit `OPEN REPORT` action in the
  detail header instead of leaving `REPORT_READY` as a passive status label.
- Change: The action opens a readable evidence-bound report view with title,
  summary, objective, best value, run count, finding and review decision;
  individual Artifacts remain available for audit.
- Verification: Frontend JavaScript syntax check passes and the report reader
  is wired to the `ResearchReport` Artifact returned by the existing API.

## 2026-08-29 - DeerFlow Windows UTF-8 subprocess fix

- Fix: DeerFlow adapter now forces UTF-8 replacement encoding in child process output to prevent GBK UnicodeEncodeError on Windows.
- Verification: MVP suite passes.

## 2026-08-30 - Async DeerFlow initialization

- Fix: Async `POST /research` now creates and queues the task before running
  literature search; DeerFlow no longer blocks the HTTP response or Mission
  page initialization.
- Verification: API returned a DeerFlow task in 0.11s with `execution_status=queued`;
  background execution then reached `REPORT_READY` in the smoke test. Full MVP
  suite passes (`82 passed`).

## 2026-08-30 - Pause/Resume UI toggle

- Change: Mission action button now switches between PAUSE and RESUME based on durable task state, and is disabled for terminal or non-running tasks.
- Verification: Frontend JavaScript syntax check passes.

## 2026-08-30 - Simplified runtime configuration UI

- Change: Removed manual Experiment Command, Experiment Workspace, and Custom Coding Command fields from the main frontend; real Coding Agent now hands execution to AutoCompute automatically.
- Change: Updated runtime note to describe automatic experiment discovery; backend advanced command options remain compatible.
- Verification: Frontend JavaScript syntax check passes.

## 2026-08-30 - Runtime visibility

- Change: Mission detail now shows Job elapsed time alongside execution, iteration, artifact, and job status, refreshed every 2.5 seconds.
- Rationale: Long experiments remain observable even when the runner emits no intermediate stdout; Docker remains an optional dependency-isolation mode.

## 2026-08-30 - Compact experiment failure display

- Change: Failed Artifact errors now filter tqdm download-progress lines and show the final 4,000 characters, prioritizing the traceback root cause while preserving full stderr in raw JSON.
- Diagnosis: The Iris/MNIST runner currently fails on a scikit-learn API incompatibility (LogisticRegression no longer accepts multi_class).

## 2026-08-30 - Compute failure repair handoff

- Fix: Failed `ExperimentRun` artifacts are now returned to the workflow as
  repairable results instead of being raised before the repair branch.
- Change: Coding Agent receives the concrete failed run (stderr/traceback,
  command, working directory and environment), performs one bounded
  `repair_experiment` action, and Compute retries the same replicate.
- Change: Task runtime records `coding_repair` and `retrying_experiment` phases
  for UI observability; a second failure terminates with the real cause.

## 2026-08-31 - Flat experiment JSON compatibility

- Fix: Compute now accepts flat JSON experiment reports containing scalar
  metrics alongside descriptive metadata, such as `score`, `accuracy`, model
  name, sample counts, and random seed.
- Fix: Automatic objective selection prefers `accuracy`, then `score`, before
  falling back to the first numeric metric; counts and seeds are not selected
  when a score is present.

## 2026-08-31 - Robust metric extraction

- Change: Compute now extracts results from noisy logs, `AUTORESEARCH_RESULT:`
  lines, fenced JSON, and embedded JSON objects.
- Change: Nested `result`, `evaluation`, and `summary` metric objects are
  supported while raw stdout remains preserved for debugging.

## 2026-08-31 - Retry-aware execution mode

- Fix: Reports now classify execution from successful candidate runs. An older
  Fake or failed attempt no longer causes a later successful real run to be
  labelled `synthetic_demo`.

## 2026-08-31 - Legacy report mode consistency guard

- Fix: The report reader detects stale `synthetic_demo` flags when the report
  contains multiple real-looking metrics (for example CV and independent test
  scores), preventing a real experiment from being presented as Fake.

## 2026-08-31 - Frontend cache bust

- Fix: Bumped the frontend `app.js` version query so browsers cannot retain the
  pre-fix report classification logic after a server restart.

## 2026-08-31 - Objective-aware iteration stopping

- Change: Critic now stops early when a candidate measurably improves over the
  baseline, retains the current hypothesis, and enters review.
- Change: Only non-improving results trigger hypothesis revision; `MAX ITER`
  remains the hard upper bound, not a mandatory number of rounds.

## 2026-08-31 - Idempotent task deletion

- Fix: Deleting a stale task ID that is already absent from persistent storage
  now succeeds instead of returning `task_not_found`; this handles old cached
  mission rows safely.

## 2026-08-31 - Stale mission row cleanup

- Fix: The frontend now removes the selected mission from its in-memory list
  immediately on delete, then refreshes from the API, so stale rows cannot
  remain actionable after an already-completed deletion.

## 2026-08-31 - PostgreSQL persistence backend

- Change: Added `PostgresArtifactStore` with automatic schema creation for
  tasks, artifacts, and jobs; `create_store()` selects it when
  `AUTORESEARCH_DATABASE_URL` is configured and retains local JSON fallback.
- Verification: Connected to the local PostgreSQL 18 service at
  `127.0.0.1:5432`, created database `autoresearch`, initialized the schema,
  and completed a store initialization smoke test.

## 2026-08-31 - Stable elapsed-time card

- Fix: The elapsed-time card is now created once and updated in place during
  polling, instead of being removed and reinserted every refresh.

## 2026-08-31 - PostgreSQL startup inheritance

- Fix: The console launcher explicitly loads the user-scoped
  `AUTORESEARCH_DATABASE_URL` before spawning the API, preventing service
  restarts from silently falling back to the local JSON store.

## 2026-08-31 - RAG module and pgvector readiness

- Change: Added `src/autoresearch/rag.py` with deterministic chunking,
  injectable embeddings, hybrid cosine/lexical retrieval, and PostgreSQL
  persistence for indexed evidence chunks.
- Change: LiteratureAgent now indexes full-text passages and exposes Top-K RAG
  candidates inside `EvidenceSet`, preserving source locators and verification
  boundaries.
- Environment: pgvector source was downloaded, but this Windows host lacks the
  MSVC Build Tools required to compile the PostgreSQL extension. The runtime
  therefore reports `postgres_jsonb_compat` until `vector` is installed, rather
  than claiming native vector search is active.

## 2026-08-31 - pgvector build attempt (D: drive)

- Action: Downloaded pgvector source from GitHub codeload to
  `F:\pgvector-src-20260831` and attempted a minimal Visual Studio Build Tools
  installation with `D:\BuildTools` and `D:\BuildToolsCache` paths.
- Result: The installer returned Windows exit code 87 and did not install the
  toolchain; no C: drive installation was left running. PostgreSQL therefore
  remains in explicit `postgres_jsonb_compat` mode until MSVC or a prebuilt
  extension is supplied.
- Verification: PostgreSQL RAG indexing and hybrid retrieval smoke test passed;
  project test suite remains green (`90 passed`).

## 2026-08-31 - Native pgvector integration path

- Change: `PostgresRAGStore` now auto-creates the `vector` extension when
  available, adds an HNSW cosine index over `vector(256)`, and runs hybrid
  pgvector + PostgreSQL FTS queries. It falls back explicitly to JSONB/in-memory
  retrieval when the server extension is unavailable.
- Build: pgvector 0.8.6 compiled successfully with the D: drive MSVC toolchain;
  copying into the protected PostgreSQL installation requires an administrator
  UAC approval and was not completed in this session.
