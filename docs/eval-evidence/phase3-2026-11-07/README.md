# Phase 3 离线可靠性公开结果

本目录只公开指标、全部逐例判分及去敏来源记录，不含原始Trace、模型/工具payload、数据库、源码备份包或凭据。前两轮v1历史证据保留在[原证据目录](../2026-11-07/README.md)，没有重写。

| 口径 | 修复前 | 修复后 |
|---|---:|---:|
| v1固定旧契约 | 144/150 | 147/150 |
| v2新幂等语义 | 147/150 | 150/150 |
| 定向假完成 | 3/9 | 0/9 |
| Recovery Success | 42/45 | 45/45 |

v1保留RT-E10的旧契约失败；v2仅更新该场景为“历史回执不变+同版本当前状态快照”。跨契约差异不能当作代码优化。

- [完整指标与分母](summary.json)
- [四种口径全部逐例判分](outcomes.json)：50例×3次×修复前后，各自保存v1/v2评分，共600条判分，实际为前后各150次运行。
- [Recovery分类](recovery.json)：自动业务恢复21/45不变，安全转人工12/45不变，安全停止9/45→12/45。
- [来源哈希与范围](provenance.json)
- [本次隔离发布验收](release-validation.json)：1017通过、18浏览器验收跳过；原工作区1043的范围不同。

源码以本分支提交为准。复用已安装依赖，在仓库根目录：

    PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 uv run python -B -m evals.reliability_runner --repetitions 3
    PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 uv run python -B -m evals.reliability_v2_runner --repetitions 3 --output-dir artifacts/evals/reliability/phase3-NEW
    PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 RUN_RPA_ROBOT_ACCEPTANCE=0 uv run python -B -m pytest -p no:cacheprovider tests packages/rpa-agent-module/tests

v1预期退出1并保留RT-E10，v2退出0。输出使用新目录。本轮没有真实模型或真实账号操作；合成耗时不是线上SLA，旧普通聊天也不自动获得业务完成保证。
