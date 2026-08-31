# AutoDeepResearch Agent 架构与职责

## 1. 系统目标与边界

AutoDeepResearch 是一个以可复现 Artifact 为核心的科研编排系统。它将“从研究问题到论文草稿”拆为可审计的阶段，并为每一步保存输入、输出、命令、环境和哈希。

当前系统能完成一轮端到端的研究工作流；最终报告固定标记为 `draft_for_human_review`。它不保证自动提出顶会级创新，也不能替代研究者对新颖性、因果性、数据合规性和结论的判断。

```text
研究方向 / 问题
  → 文献与证据
  → 假设
  → 实验设计与代码
  → 基线与候选实验
  → 统计分析
  → 审稿检查
  → 可复现性打包
  → 人工审查论文草稿
```

## 2. 编排与基础设施层

## 2A. 对外 Agent 部署边界

为避免把每个内部职责都部署成独立服务，建议对外只保留四个智能角色；实验执行器是
编排器内的本地基础设施，不单独部署成 Agent：

```text
1. Orchestrator：科学流程、状态机、预算和 Artifact
2. DeerFlow Research：检索、精读、文献推理和创新候选
3. Codex Experiment：代码实现、测试、调用本地实验执行器并读取结果
4. Writer / Reviewer：论文写作、审稿反驳和修订建议
```

第 5 个角色内部可以继续分成 `ReviewerAgent` 与 `ReportWriterAgent`；它们逻辑上分工不同，但在 PC / 普通服务器部署时可以放在同一个进程或服务中。当前代码已经有 `ReportWriterAgent`，但它主要生成证据绑定的结构化报告；完整投稿级 `PaperWriter` 是后续增强方向。

Writer/Reviewer 已吸收 Nature skills 的关键约束：证据先于表述、缺失事实写成
`AUTHOR_INPUT_NEEDED`、量化结果绑定 ExperimentRun/Finding、审稿按 originality /
importance / technical soundness / evidence / readability 轴检查，统计报告区分独立
实验单位、效应量和不确定性。它们作为本地 Artifact 契约执行，不依赖额外的远端服务。

### ResearchWorkflow（主编排器）

职责：根据状态机调用各阶段 Agent，校验输入输出契约，在失败时停止后续阶段，并记录完整状态迁移。

阶段顺序：

```text
DRAFT
→ SEARCHING
→ EVIDENCE_READY
→ HYPOTHESES_READY
→ AWAITING_APPROVAL
→ BASELINING（可选）
→ IMPLEMENTING
→ RUNNING
→ ANALYZING
→ REVIEWING
→ REPORT_READY
```

### ArtifactStore（不可变科研记录）

职责：以 JSON 保存每个 Artifact；同 ID 的内容不可被覆盖。每条记录包括 producer、inputs、status、payload、created_at 和 content hash。

人类阅读入口：每个 CLI 任务完成后同时写出 `<store>/report.md`；JSON 仍保留用于复现和审计。

### 队列与控制平面

职责：本地单进程队列、重试、取消、孤儿任务恢复、任务状态与 Artifact 查询。HTTP API 可提交、恢复、取消任务和读取结果。

边界：当前 API 是本地开发控制平面，尚未提供生产级认证、TLS、多机调度或分布式队列。

### A2A Adapter（Agent-to-Agent）

职责：通过 `/.well-known/agent-card.json` 发现能力，并通过 `/message:send` 调用兼容远程 Agent。它校验返回 Artifact 的 kind、输入引用和哈希。

边界：实现了轻量 HTTP+JSON 开发协议；未部署任何独立的生产远程 A2A 服务。

## 3. 研究 Agent 层

| 阶段 | Agent / 模块 | 当前接入方式 | 职责 | 输出 |
|---|---|---|---|---|
| Literature | `LiteratureAgent` | 本地 | 汇总检索源、规范化记录、去重、保存原始响应快照 | `EvidenceSet` |
| Literature source | Crossref / arXiv | 在线 API | 检索结构化元数据与公开摘要 | 文献记录 |
| Literature source | DeerFlow | 已真实接通 | 多步骤调研、搜索与资料归纳；经 JSON 子进程适配器交给 LiteratureAgent | 文献记录、引用链接、事件快照 |
| Literature source | DeepResearch | 已有子进程接口，需显式配置命令 | 接入外部 DeepResearch 工具 | 文献记录 |
| Evidence mapping | `ClaimEvidenceAgent` | 本地 | 将假设与摘要/全文候选片段建立可审计链接 | `ClaimEvidenceMap` |
| Hypothesis | `FakeHypothesisAgent` / 外部 Hypothesis adapter | 本地默认；可替换子进程/A2A | 基于问题与证据写出可检验假设 | `HypothesisSet` |
| Coding / Experiment | `CodexCodingAgent` | 已真实接通 Codex CLI | 读取实验上下文、修改实验仓库、运行约定测试；可由编排器随后调用本地执行器 | `CodeRevision` |
| Coding | `ClaudeCodeAgent` | 可选 CLI | 与 Codex 角色相同，作为可替换 Coding Agent | `CodeRevision` |
| Experiment runner | `LocalComputeAgent` | 编排器内本地子进程 | 被 Coding/Experiment 阶段调用，运行 baseline 和 candidate 命令，解析严格 JSON 指标 | `ExperimentRun` |
| Experiment runner | `DockerComputeAgent` | 可选 Docker | 被 Coding/Experiment 阶段调用，以受限容器执行实验 | `ExperimentRun` |
| Analysis | `MetricsAnalysisAgent` / 外部 adapter | 本地默认；可替换 | 汇总重复实验，计算均值、标准差、标准误和描述性 baseline delta | `Finding` |
| Critic | `ResearchCriticAgent` | 本地默认；可替换 A2A | 根据分析结果判断继续、修改假设或停止；受 `--iterations` 预算约束 | `ResearchDecision` |
| Reviewer | `EvidenceReviewAgent` / 外部 adapter | 本地默认；可替换 | 标识证据不足、结论越界和需要人工复核的风险 | `ReviewReport` |
| Reproducibility | control-plane | 本地 | 校验 Artifact 哈希、引用关系、命令和结果链 | `ReproducibilityPackage` |
| Report | `ReportWriterAgent` | 本地默认；可替换 | 将证据、实验、分析、审稿和可复现信息组织为草稿 | `ResearchReport` + `report.md` |

## 4. DeerFlow 的具体位置

DeerFlow 是深度调研子 Agent，不是整个系统的总编排器。

```text
AutoResearch LiteratureAgent
  → DeerFlowSource（子进程）
  → DeerFlow CLI（JSON 事件流）
  → Codex relay / gpt-5.6-terra
  → 规范化文献与引用链接
  → EvidenceSet
```

已完成的兼容处理：

- 使用 Codex relay：`https://chat1.sorryios.io/codex`（不附加 `/v1`）。
- 使用模型：`gpt-5.6-terra`。
- API Key 仅从本机环境变量读取，不写入仓库。
- DeerFlow 的 system message 会经本地兼容 shim 转为该 relay 可接受的消息格式。
- 已用真实请求验证 DeerFlow 返回 `DEERFLOW_OK`。

## 5. Codex 的具体位置

Codex 是 Coding/Experiment Agent，不是研究总控。

```text
主编排器给出实验上下文
  → CodexCodingAgent（写代码、调试并请求本地运行）
  → Codex CLI
  → 修改 / 检查实验仓库
  → CodeRevision Artifact
  → LocalCompute/DockerCompute 执行实际实验（无独立大模型）
```

主编排器仍负责研究问题拆解、阶段顺序、证据约束、实验比较、审稿和报告组织；Codex 只在“实现与验证代码”边界内工作。

## 6. 当前缺失的顶会级研究能力

下列能力尚未构成完整自动化闭环，不能把它们视为已实现：

1. **论文精读与创新发现 Agent**：全文/PDF 合法获取、逐篇方法-实验-局限性抽取、横向对比和可验证 gap 发现。
2. **强假设生成 Agent**：多轮生成、反驳、优先级排序，而非仅生成通用假设。
3. **PC / 普通服务器实验平台**：单机 CPU/GPU、多数据集、小规模超参搜索、实验预算、并发上限与断点恢复。集群和大规模算力调度不在当前目标范围内。
4. **严格统计 Agent**：显著性检验、置信区间、效应量、数据泄漏与实验设计检查。
5. **科学绘图 Agent**：生成论文级图、表、消融图和可投稿格式导出。
6. **多轮审稿-返修 Agent**：不同 Reviewer 独立审稿、意见冻结、返修实验和论文重写。
7. **论文写作 Agent**：完整 related work、methods、results、discussion 与引用格式化，而不仅是结构化报告。

## 7. 建议补齐的 Literature Intelligence 子流程

这是把“能搜索”升级为“能发现创新点”的关键模块：

```text
检索候选论文
→ 按需打开合法全文 / PDF（默认不批量下载）
→ Paper Reader：按需逐篇提取问题、方法、数据、指标、结论、局限性
→ Paper Card：生成可追溯的结构化阅读卡
→ Comparison Matrix：跨论文比较方法、假设、数据、结果与失败场景
→ Gap Finder：提出矛盾、空白或未充分验证的候选创新点
→ Innovation Critic：检查是否已有工作、是否可验证、成本和风险
→ Innovation Brief：输出优先级明确的实验计划
→ Hypothesis / Coding / Compute
```

该模块的预期输出：

- `paper_cards/`：逐篇精读卡与原文定位。
- `comparison_matrix.md`：相关工作横向比较。
- `innovation_brief.md`：创新候选、差异化、证据、风险与反证实验。
- `research_plan.md`：baseline、数据、指标、消融、算力和成功判据。

### 全文访问策略

默认采用“检索 → 筛选 → 按需打开全文”的策略，而不是批量下载论文：

1. 先用元数据和摘要完成去重、相关性评分和粗筛；
2. 只对入选的少量论文（例如 3~5 篇）打开开放获取页面、arXiv PDF 或用户已授权的机构页面；
3. Reader 在本地提取必要段落、页码/章节定位和 SHA-256，不长期保存整批 PDF；
4. 如果用户明确要求离线复现或归档，再对指定论文执行合法下载；
5. 无法访问全文时保留 `abstract_only` / `metadata_only`，不把摘要推断写成全文结论。

这样可以减少磁盘和网络开销，同时避免将大批受版权保护的文件复制到项目目录。

## 8. 使用方式：从方向到一次研究运行

建议用户至少提供：

```text
方向：例如“小样本医学影像分类”
目标：例如“MICCAI / NeurIPS 风格的可验证研究”
实验仓库：本地路径或可运行模板
数据集：路径、访问方式、许可约束
资源：GPU 型号/数量、时间和预算
指标：主指标、次指标与成功门槛
对比方法：已有 baseline 或允许系统选择的范围
```

系统应先产出 Literature Intelligence 的研究计划，等待人类批准，再授权 Coding 和 Compute 阶段执行。这样可以避免在未确认研究问题、数据合规或资源预算时盲目消耗算力。

## 9. 自适应反馈循环（已实现）

当 `--iterations` 大于 1 时，每轮分析后都会运行 Critic。若结果与 baseline
没有差异，Critic 产生 `revise_hypothesis`，系统以 EvidenceSet、Finding 与该决策
重新调用 Hypothesis Agent，并刷新 claim-to-evidence map；下一轮 Coding 获得更新后的
研究上下文。Critic 也可通过 A2A 返回 `stop_early`，使低价值分支直接进入 Reviewer。
最新决策同时进入 Reviewer、ResearchReport 和 `report.md`。循环始终受
`--iterations` 限制，避免无限试验。

## 10. 资源策略：PC / 普通服务器优先

项目以研究者个人 PC 或一台普通服务器为主要运行环境，不以 Kubernetes、Slurm、
多机训练或大规模 GPU 集群为前提。实验设计应优先采用可在有限资源上验证的策略：

- 在 CPU 或单张 GPU 上运行可复现实验；
- 先用小样本、代理数据或短训练筛选想法，再批准高成本复验；
- 限制并发、总迭代数、单次命令超时和磁盘占用；
- 支持中断后从 Artifact 与任务状态恢复；
- 明确记录设备、随机种子、环境、预算和降级实验设置。

这不降低科学标准：最终结论仍需要独立数据、足够重复、合理对照和人工审查；它只将研究计划约束在可由个人或普通实验室承担的资源范围内。

## 11. 当前待办路线图（PC / 普通服务器优先）

1. **文献智能（最高优先级）**：实现全文/PDF 的合法获取、Paper Card、跨论文对比矩阵和 Innovation Brief；这是“给一个方向”也能形成有根据研究计划的基础。
2. **研究设计 Agent**：将创新候选转成 baseline、数据集、指标、消融、反证实验、时间/显存预算和明确成功判据。
3. **自适应研究闭环**：从当前单一路径 Critic 扩展为有限数量的候选假设分支、失败归因、分支选择和停止准则。
4. **单机实验严谨性**：加入随机种子控制、资源/磁盘预算、置信区间、显著性检验、效应量、数据泄漏与伪重复检查。
5. **论文与图表**：论文级表图、完整论文各章节、多个独立 Reviewer 与基于意见的补实验/修订循环。
6. **可选 A2A 部署**：提供在普通服务器部署 Literature、Critic、Reviewer 等远端 Agent 的模板，并补认证与访问边界；不是集群调度。

## 12. 80% 目标：PC 可用的科研助手

这里的 80% 指：给定一个相对明确的研究方向和本地可用数据，系统能够在普通 PC/服务器上完成“文献证据 → 研究计划 → 实验 → 统计 → 论文草稿”的大部分工作；不表示自动保证顶刊录用或自动证明原创性。

### 80% 验收标准

1. **文献证据链**：对筛选出的 3~5 篇论文按需打开全文，生成 Paper Card；每个关键主张都有来源级别和页码/章节定位。
2. **研究设计链**：从候选 gap 生成明确的 baseline、candidate、消融、指标、失败条件和 PC 资源预算。
3. **实验链**：本地可恢复执行多个随机种子和有限实验分支，输出置信区间、效应量和数据泄漏检查。
4. **反馈链**：结果不支持假设时，Critic 进行失败归因并选择修改假设、补实验或早停。
5. **写作链**：Writer 生成完整论文结构（摘要、引言、相关工作、方法、实验、讨论、局限性、参考文献），每个结果绑定 Artifact。
6. **审稿链**：至少两个相互独立的 Reviewer 检查新颖性证据、实验充分性、统计和结论越界，并形成修订清单。
7. **可复现链**：一条命令或 manifest 可以重放关键实验，报告和原始 Artifact 可互相追溯。

### 实施顺序

```text
Phase A：Paper Reader + Paper Card + Evidence binding
Phase B：Research Planner + gap 反证搜索
Phase C：本地统计、防错和有限分支实验
Phase D：完整 Paper Writer + 多 Reviewer + 修订循环
```

每个 Phase 都必须有真实小课题验收；没有通过验收的模块不计入“80%”。
