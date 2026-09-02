# AutoDeepResearch 项目阅读指南

这份文档用于帮助开发者从整体到局部理解 AutoDeepResearch。建议先通读架构和一次 Mission 的执行路径，再按“推荐阅读顺序”逐个打开源文件。

## 一句话定义

AutoDeepResearch 是一个 Artifact-first 的科研控制平面：它把文献研究、证据整理、假设设计、代码实现、实验执行、结果分析、审查和报告写作组织成一个有状态、可迭代、可追溯的闭环。

DeerFlow 和 Codex 是其中的能力适配器；真正负责流程控制的是 `ResearchWorkflow`。

## 总体架构

```text
用户问题
  ↓
FastAPI / CLI
  ↓
ResearchTask + execution profile
  ↓
ResearchWorkflow（控制平面）
  ↓
各阶段 Agent / Compute Runner
  ↓
Artifact 持久化
  ↓
下一阶段、循环分支或最终报告
```

一次任务的主链路：

```text
DRAFT
  → SEARCHING
  → EVIDENCE_READY
  → HYPOTHESES_READY
  → AWAITING_APPROVAL
  → IMPLEMENTING
  → RUNNING
  → ANALYZING
  → REVIEWING
  → REPORT_READY
```

结果不佳时的循环分支：

```text
ANALYZING
  → ResearchCriticAgent
      ├─ revise_hypothesis
      │    → 新 HypothesisSet
      │    → ClaimEvidenceMap 更新
      │    → ITERATING
      │    → IMPLEMENTING
      │    → RUNNING
      ├─ stop_early
      │    → REVIEWING
      └─ retain_and_test
           → 下一轮实验，或预算耗尽后 REVIEWING
```

状态机和 Agent 是两层概念：状态表示任务在控制平面中的阶段；Agent 表示执行某项能力的实现。例如 `IMPLEMENTING` 是状态，`CodexCodingAgent` 是执行该状态工作的 Agent，产物是 `CodeRevision`。

## 核心数据对象

### ResearchTask

文件：`src/autoresearch/models.py`

`ResearchTask` 代表一个 Mission 的当前状态，主要字段包括：

- `question`：科研问题
- `state`：研究阶段
- `history`：状态转换记录
- `artifacts`：该任务关联的 Artifact ID
- `iteration` / `max_iterations`：当前轮次与最大轮次
- `replicates`：每轮独立实验次数
- `objective_metric` / `objective_direction`：优化指标和方向
- `best_value`：当前最佳指标
- `execution_status`：队列/作业层状态
- `error`：任务级错误
- `runtime`：当前命令、工作目录、依赖审批等运行信息

`ResearchTask` 保存“任务走到哪一步”，不保存 Agent 的全部输出内容。

### Artifact

文件：`src/autoresearch/models.py`

每个阶段都应该产出一个结构化 Artifact，而不是只返回自然语言。常见类型：

```text
EvidenceSet              文献和证据集合
LiteratureIntelligence   文献卡片、比较矩阵、研究计划
ResearchPlan             可执行研究计划
HypothesisSet            可检验假设
ClaimEvidenceMap         假设与证据候选的映射
CodeRevision              代码实现或修复结果
ExperimentRun            一次真实实验运行
Finding                  实验分析结果
ResearchDecision         Critic 的继续/修订/停止决策
ReviewReport             审查结果
ReproducibilityPackage   可复现性验证包
ResearchReport           最终报告
```

每个 Artifact 都包含：

```text
artifact_id
kind
producer
inputs
payload
content_hash
status
created_at
```

其中 `inputs` 建立 provenance 图，`content_hash` 用于确认内容没有被悄悄修改。

## 推荐源代码阅读顺序

### 1. README

文件：`README.md`

先了解启动方式、配置参数、状态流转、A2A、RAG、依赖审批和 API。

### 2. 数据模型

文件：`src/autoresearch/models.py`

先看 `ResearchState`、`Artifact`、`ResearchTask` 和 `transition()`。这里定义了系统允许的状态图和数据契约。

### 3. 总编排器

文件：`src/autoresearch/workflow.py`

这是最重要的文件，重点读：

- `_agent()`：按阶段找到 Agent
- `_check_cancellation()`：检查取消和暂停
- `_run_after_approval()`：执行实现、实验、分析和迭代循环
- `run()`：从 DRAFT 启动任务并处理异常
- `_update_best()`：更新目标指标

### 4. Agent 组装

文件：`src/autoresearch/cli.py`

重点读 `build_agents()`。它决定具体实现接到哪个阶段：

```text
fake       → Fake Agent
codex      → CodexCodingAgent
claude     → ClaudeCodeAgent
command    → SubprocessCodingAgent
a2a URL    → A2AHttpAgent
```

同时读 `run_research()`、`resume_research()` 和 `build_execution_profile()`，理解任务创建、恢复和配置 hash。

### 5. 本地科研流程 Agent

文件：`src/autoresearch/agents.py`

- `LiteratureAgent`：调用文献源，整理 EvidenceSet，并把全文段落送入 RAG
- `FakeHypothesisAgent`：默认的本地假设实现；也可换成 subprocess/A2A
- `ClaimEvidenceAgent`：把假设与全文/RAG 候选做 lexical triage，明确标记为未验证
- `MetricsAnalysisAgent`：聚合实验指标并生成 Finding
- `ResearchCriticAgent`：根据结果决定继续、修订假设或提前停止
- `EvidenceReviewAgent`：审查证据、实验和限制
- `ReportWriterAgent`：生成证据边界明确的报告

### 6. Coding Agent 适配器

文件：`src/autoresearch/coding.py`

- `CodexCodingAgent`：调用 Codex CLI 在工作区实现/修复实验
- `ClaudeCodeAgent`：调用 Claude Code CLI
- `SubprocessCodingAgent`：调用任意符合 JSON 输入输出契约的本地命令

Coding Agent 消费研究计划、假设、上一轮结果和失败日志，产出 `CodeRevision`。它负责写和修代码，不负责自报实验指标。

### 7. 实验执行器

文件：`src/autoresearch/compute.py`

- `LocalComputeAgent`：在本机运行实验命令
- `DockerComputeAgent`：在 Docker 中运行实验
- `AutoComputeAgent`：根据工作区自动发现实验入口
- `extract_result()`：从 stdout 提取结构化 metrics

Compute 是验证边界：它实际执行代码、记录环境和 stderr，并产出 `ExperimentRun`。Coding 和 Compute 分离可以避免代码 Agent 自己声称实验结果。

依赖流程是：

```text
检查 requirements.txt
  → 缺依赖
  → AWAITING_DEPENDENCY_APPROVAL
  → 用户批准
  → python -m pip install -r requirements.txt
  → 恢复当前实验
```

### 8. 文献和 RAG

文件：`src/autoresearch/literature.py`、`src/autoresearch/fulltext.py`、`src/autoresearch/rag.py`

文献层负责 Crossref、arXiv、DeerFlow 等来源及来源快照；全文层负责 HTML、TXT、Markdown、PDF 的段落抽取；RAG 层负责：

- 文本切块
- embedding
- PostgreSQL chunks 持久化
- pgvector HNSW 余弦检索
- PostgreSQL Full-Text Search
- 向量和关键词混合召回

当前 PostgreSQL 已启用 pgvector `0.8.6`，RAG 后端为原生 `pgvector`。检索到的内容仍然是候选证据，不能自动等同于已验证的科学结论。

### 9. 存储、队列和 API

文件：`src/autoresearch/storage.py`、`src/autoresearch/queue.py`、`src/autoresearch/api.py`

- `storage.py`：本地 JSON store 与 PostgreSQL store
- `queue.py`：后台 Job、原子任务锁、暂停、取消、重试和 orphan recovery
- `api.py`：创建任务、查询状态、读取 Artifact、审批依赖、暂停/恢复、取消、删除和报告接口

前端文件：`frontend/app.js`

前端只负责提交任务、轮询状态、渲染 Trace/Artifact/报告和触发控制操作；科研逻辑在后端。

## 一次真实 Mission 怎么走

以“简单实现鸢尾花分类”为例：

```text
POST /research
  ↓
创建 ResearchTask(DRAFT)
  ↓
LiteratureAgent / DeerFlow
  ↓ EvidenceSet、LiteratureIntelligence、ResearchPlan
Hypothesis Agent
  ↓ HypothesisSet
ClaimEvidenceAgent
  ↓ ClaimEvidenceMap
CodexCodingAgent
  ↓ CodeRevision
Local/Docker Compute
  ↓ ExperimentRun
MetricsAnalysisAgent
  ↓ Finding
ResearchCriticAgent
  ↓ ResearchDecision
EvidenceReviewAgent
  ↓ ReviewReport
ReproducibilityPackage
  ↓
ReportWriterAgent
  ↓ ResearchReport(REPORT_READY)
```

实验失败时：

```text
ExperimentRun(status=failed)
  → 失败日志、traceback、环境信息传给 Codex
  → repair_experiment
  → 新 CodeRevision
  → Compute 重跑
```

当前修复次数是有界的，避免无限自动修复。

## 为什么这样设计

### 为什么状态机和 Agent 分离？

状态机负责流程控制和恢复；Agent 负责能力执行。这样可以把 Codex 替换成 Claude、自定义命令或远端 A2A Agent，而不重写科研流程。

### 为什么使用 Artifact？

纯文本输出无法可靠追踪输入、生产者和版本。Artifact 让每个中间结论都能关联来源、输入和 hash。

### 为什么 Coding 和 Compute 分离？

代码生成不等于实验验证。独立 Compute Runner 实际运行代码并捕获指标，减少自报结果和不可复现问题。

### 为什么结果不好时优先改假设而不是重新检索？

原始文献证据通常仍然有效。系统优先复用 EvidenceSet，只更新假设、代码和实验；重新检索可以作为未来扩展分支。

### A2A 在哪里？

文件：`src/autoresearch/a2a.py`

它提供 Agent Card、`POST /message:send`、远端 Artifact 校验和 A2A Agent 接入。当前 DeerFlow 与 Codex 主路径是 CLI Adapter；A2A 是远程扩展边界，不是这两个本地 CLI 的原生通信方式。

## 建议的学习实践

### 第一次：只看结构

```powershell
cd F:\AutoDeepResearch
rg --files src\autoresearch
```

先记住：

```text
models.py → workflow.py → cli.py → agents.py
                         ↘ coding.py / compute.py / rag.py
                           storage.py / queue.py / api.py
```

### 第二次：跑最小 Fake 流程

```powershell
$env:PYTHONPATH = "src"
python -m autoresearch.cli "简单实现鸢尾花分类" `
  --literature fixture `
  --iterations 2 `
  --replicates 1 `
  --store .autoresearch
```

观察任务的 `state`、`history`、Artifact 列表、`ExperimentRun`、`Finding`、`ResearchDecision` 和 `ResearchReport`。

### 第三次：读真实 Agent 配置

```powershell
python -m autoresearch.cli "简单实现鸢尾花分类" `
  --literature deerflow `
  --deerflow-cwd "F:\AutoDeepResearch\third_party\deer-flow-main" `
  --coding-agent codex `
  --coding-cwd "F:\ads_test" `
  --iterations 2 `
  --replicates 1 `
  --store .autoresearch
```

### 第四次：专门读失败任务

失败任务最能解释系统的边界。重点查看：

```text
task.error
ExperimentRun.payload.stderr
environment_error
dependency_request
repair_attempt
execution_profile_hash
```

### 第五次：读测试

测试文件是行为说明书。优先阅读：

- `tests/test_mvp.py`
- `tests/test_dependency_approval.py`

重点关注状态转换、Agent contract、队列、暂停恢复、依赖审批、RAG 和失败重试测试。

## 面试时的一句话总结

> AutoDeepResearch 是一个 Artifact-first 的科研控制平面，把文献研究、假设生成、代码实现、实验执行、结果分析、审查和写作组织成有状态、可迭代、可追溯的闭环；DeerFlow 和 Codex 是可替换的外部能力适配器，Workflow 才是系统核心。

## 两个容易被问到的术语

### Agent Contract 是什么？

Agent Contract 不是某个额外的模型，而是 Agent 与控制平面之间约定的输入/输出接口。它规定：Agent 接收哪些输入 Artifact，必须产出什么类型的 Artifact，失败时如何报告，以及下一个 Agent 可以依赖哪些字段。

例如 Coding Agent 的最小契约可以理解为：

```text
输入：ResearchPlan、HypothesisSet、上一轮 ExperimentRun（如有）
输出：CodeRevision，或结构化失败信息
交接：execution_contract = {command, cwd, timeout}
```

这样 Codex、Claude 或本地自定义命令只要遵守同一个接口，就能替换；Workflow 不需要知道具体 CLI 的参数细节。面试时可以说“用契约隔离 Agent 实现与流程编排”，不要说成“实现了完整的 A2A 标准协议”。

### Artifact-first 数据模型是什么？

Artifact-first 指的是：阶段之间优先传递结构化、可持久化的产物，而不是只传一段聊天文本。`EvidenceSet`、`HypothesisSet`、`CodeRevision`、`ExperimentRun`、`Finding` 和 `ResearchReport` 都是 Artifact。

每个产物保留 `artifact_id`、`kind`、`producer`、`inputs`、`payload`、`content_hash` 和时间戳，因此可以回答“谁生成的、依据什么、哪一轮产生、是否被修改”。它是可追溯和可恢复的工程数据模型，不等于把所有内容都存成向量，也不等于自动证明科研结论正确。

## 简历表述校准

你当前版本的方向是对的，但建议做三点校准：

1. `A2A` 应写成“实现 A2A 兼容扩展接口/远端 Agent 接入能力”，因为 DeerFlow 与 Codex 的主路径仍是本地 CLI Adapter，不要暗示它们已经通过原生 A2A 通信。
2. `Docker` 应写成“支持 Docker Compute 隔离执行”，只有在简历中确实展示过容器运行时才写；否则可放到扩展能力中。
3. `Agent Contract` 和 `Artifact-first` 要配一句结果导向的解释，否则容易变成术语堆砌。重点强调替换 Agent、失败恢复、来源追溯和实验可复现。

### 推荐压缩版

**AutoDeepResearch｜多智能体协同的端到端自动科研平台**  
`Python · PostgreSQL · pgvector · RAG · DeerFlow · OpenAI Codex CLI · Docker · A2A`

**项目概述：** 面向普通 PC 与服务器，将文献调研、假设设计、代码实现、实验验证、结果分析、审查和论文草稿生成串成可迭代、可追溯的科研闭环。

**科研流程编排：** 设计状态机和统一 Agent Contract，以结构化 Artifact 驱动阶段交接；实验结果不达标时由 Critic 触发假设修订、代码修复与实验复跑，并支持暂停恢复、失败重试和提前终止。

**Agent 与真实执行：** 适配 DeerFlow 深度研究框架和 Codex Coding Agent；由独立 Local/Docker Compute 实际执行实验、采集指标和错误日志，区分“代码生成”和“实验验证”。

**可视化与扩展：** 构建任务控制 API 与 Web 控制台，支持 Mission、实时 Trace、Artifact、报告查看和依赖审批；实现统一适配层以隔离 CLI/API 差异，并提供 A2A Agent Card 与远端 Artifact 交互接口，已具备替换本地 Agent 或接入远端 Agent 的扩展能力。

**证据与复现：** 基于 PostgreSQL + pgvector 实现向量/全文混合检索，保留来源快照、Claim-Evidence 映射、代码版本、运行环境和命令，生成可审计的复现材料。

**面试提醒：** 如果没有可靠线上指标，不要写“准确率提升 xx%”；可以写测试覆盖数、真实 smoke test、支持的状态/恢复能力等可验证事实。A2A 建议放在技术栈末尾，作为远端扩展能力而非项目主卖点。
