# 2026-11-07 Agent 离线可靠性评测公开证据

固定数据集50例，每例3次，所有版本都保留失败。公开文件包含全部450次逐例判分、失败原因、指标、Markdown报告和去敏manifest；移除本机标识、元数据精确采集时刻及本地实验路径。完整原始JSON和Trace仅留本地，不上传压缩归档。来源与哈希见[summary.json](summary.json)。

| 版本 | 场景契约 | 状态一致性 | 仍失败的场景 |
|---|---:|---:|---|
| 原始 | 138/150 | 90/99 | RT-E08、09、10、12 |
| RT-E09 | 141/150 | 93/99 | RT-E08、10、12 |
| 第二轮 | 144/150 | 96/99 | RT-E10、12 |

- [原始报告](original/report.md) · [150次结果](original/outcomes.json)
- [RT-E09报告](rt-e09/report.md) · [150次结果](rt-e09/outcomes.json)
- [第二轮报告](phase2/report.md) · [150次结果](phase2/outcomes.json)
- [完整对照与本地测试记录](comparison.json)

## 可复现源码与边界

本评测增量以已公开的codex/job-demo-acceptance@6888395为基础，不包含原工作区正在进行的独立Runtime迁移及相应依赖/锁文件变更。冻结评分器和用例原样保留；源码以本分支提交为准。历史manifest记录当时dirty源码和依赖指纹，不能当成新发布提交的干净验收。

在仓库根目录：

    uv sync --frozen
    PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 uv run python -B -m evals.reliability_runner --repetitions 3
    PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 RUN_RPA_ROBOT_ACCEPTANCE=0 uv run python -B -m pytest -p no:cacheprovider tests packages/rpa-agent-module/tests

Benchmark预期仍返回1并保留RT-E10、RT-E12。RT-E10采用历史回执与当前状态分开的新增字段，原v1断言未改；RT-E12通用文本输出未改。18项浏览器验收按显式条件跳过。本批不调用真实模型或真实账号，不将离线耗时当作线上SLA。

1008通过/18跳过是原工作区完整测试记录，包含本批之外的并行Runtime测试。公开隔离快照的结果另记在release-validation.json中，不借用该总数。

本分支只发布评测增量及必要业务修复，不合并任何分支，不操作生产数据。原始SQLite、浏览器资料、工作区diff、私有日志、依赖目录和源码备份包均不上传。

自动审批曾因可识别机器元数据、以及无法核验完整派生压缩包内容而拒绝上传。已清理元数据，并将完整报告与Trace排除在公开批次之外；本地证据和判分标准未改。
