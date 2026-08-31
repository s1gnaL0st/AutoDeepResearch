# AutoResearch Console

这是一个本地优先的单页控制台，采用黑白基底、酸绿色信号色和等宽数据字体。
它不包含第三方构建依赖，直接由静态 HTTP 服务提供，并通过 REST 连接现有控制平面 API。

## 启动

在仓库根目录分别启动：

```powershell
$env:PYTHONPATH = "src"
python -m autoresearch.api --store .autoresearch --port 8090
python -m http.server 5173 --directory frontend
```

打开 <http://127.0.0.1:5173>。左下角 Connection 可以切换 API 地址。

也可以从仓库根目录一次启动两个本地服务：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_console.ps1
```

## 当前能力

- 创建 fixture / DeerFlow / live literature 任务
- 设置迭代次数和重复实验数
- 在 `REAL RUNTIME CONFIG` 中选择 Demo、Codex CLI 或 Claude Code CLI，填写代码工作区、实验命令和实验工作区
- 可设置目标指标（例如数字识别任务用 `accuracy`，不要使用默认的 `score` 覆盖真实指标）
- 轮询任务状态并展示阶段 pipeline
- 任务详情实时显示当前执行动作（如“正在检索文献”“实验执行器正在运行实验”）、旋转状态指示和迭代进度
- 展开 `TRACE` 可查看完整状态转换时间线；打开报告可查看每个 iteration/replicate 的运行轨迹和指标
- 按时间顺序浏览 Evidence、Hypothesis、Code、Experiment、Review、Report Artifacts
- 查看单个 Artifact 的结构化 payload
- 右上角主题开关：黑色默认主题 / 白色阅读主题，选择会保存在浏览器本地

前端只调用本地 API；API 的 loopback 限制和 Artifact 审计仍由后端负责。
真实 CLI 的认证信息不进入前端，继续由本机环境变量提供。配置会保存在浏览器本地，便于重复运行；首次使用真实模式前请确认工作区和实验命令正确。
