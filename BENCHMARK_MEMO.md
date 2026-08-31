# AutoResearch Agent 能力测评备忘录

## 1. 外部 Benchmark 对照

| 能力 | Benchmark | 主要测评内容 |
|---|---|---|
| 通用工具 Agent | AgentBench、GAIA | 任务规划、工具调用、多步任务完成 |
| Coding Agent | SWE-bench Verified、HumanEval、Terminal-Bench | 修复代码、生成代码、终端执行与验证 |
| 网页/检索 Agent | WebArena、BrowserGym、BrowseComp | 搜索、浏览器操作、信息定位 |
| 计算机操作 Agent | OSWorld | 文件、终端和 GUI 跨应用操作 |
| 长流程工具 Agent | τ-bench | 多轮工具调用、状态一致性 |
| 科研 Agent | PaperBench、ResearchBench、DeepResearch Bench 类任务 | 文献检索、证据归因、研究计划和报告质量 |

## 2. 建议自建 AutoResearch-Bench

针对本项目的端到端目标，建立本地可复现任务集，而不是只依赖通用榜单。

标准流程：

```text
科研问题
→ 检索可验证文献
→ 生成可检验假设
→ 编写可运行实验
→ 得到真实指标
→ 输出证据可追溯结论
```

## 3. 评价指标

- **文献检索：**引用准确率、全文可访问比例、证据覆盖率
- **假设生成：**可检验性、变量定义完整度、失败条件明确度
- **Coding：**实验可运行率、测试通过率、代码修改正确率
- **Compute：**真实指标解析率、实验可复现率、超时/失败诊断率
- **科研推理：**结论证据一致性、是否超出证据、反事实/反驳处理能力
- **系统工程：**端到端成功率、总耗时、任务恢复率、成本

## 4. 第一批本地任务集

先准备 10–20 个普通 PC 可运行任务，例如：

- MNIST 手写数字识别：SVM、KNN、CNN 对比
- UCI 数据集分类/回归 baseline
- 小规模文本分类与特征方法对比
- 图像特征提取与传统分类器对比
- 超参数或数据增强消融实验

每个任务固定：数据集、允许依赖、资源预算、目标指标、验收脚本和预期输出格式。

## 5. 后续实现建议

增加 `benchmarks/` 目录，每个任务包含：

```text
task.yaml        # 问题、数据、指标、资源限制
grader.py        # 自动验收与评分
reference/       # 基线与参考结果
README.md        # 任务说明
```

最终输出总分和分项分数，并保存完整 Trace、Artifacts、实验日志和失败原因。

