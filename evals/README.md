# 第四周评测框架

评测框架把 Code、销售和 Browser 的固定任务统一成同一种结果记录，比较：

```text
case pass rate
task success rate
cleanup rate
P50/P95 duration
failure type
```

先运行一个低成本烟雾评测，每个套件只取第一个用例：

```bash
uv run python -m evals.runner --suite all --max-cases 1
```

运行完整的一轮固定用例：

```bash
uv run python -m evals.runner --suite all
```

当前固定矩阵为 Code 30 个、Browser 30 个、销售 3 个；Browser 用例是
`example.com` 同源的 200/404 URL 矩阵，适合测导航和清理，不代表 30 个不同网站。

分别运行计划中的 30 个 Code 或 Browser 用例：

```bash
uv run python -m evals.runner --suite code --concurrency 3
uv run python -m evals.runner --suite browser --concurrency 3
```

重复 3 次：

```bash
uv run python -m evals.runner --suite all --repetitions 3
```

并发运行（例如同时运行 3 个用例）：

```bash
uv run python -m evals.runner --suite all --repetitions 2 --concurrency 3
```

运行 30 个实例（10 个固定用例 × 3 轮），并记录并发、成本和 95% Wilson 区间：

```bash
uv run python -m evals.runner --suite all --repetitions 3 --concurrency 3 \
  --cpu-cores 2 --memory-gib 4 \
  --cpu-price-cny-per-core-second 0.000081 \
  --memory-price-cny-per-gib-second 0.000025
```

如果已从腾讯云计费页确认 CPU/内存单价，可以传入资源规格和价格做估算：

```bash
uv run python -m evals.runner --suite all \
  --cpu-cores 2 \
  --memory-gib 4 \
  --cpu-price-cny-per-core-second 0.000081 \
  --memory-price-cny-per-gib-second 0.000025
```

报告中的成本是“沙箱墙钟生命周期秒数 × CPU/内存单价”的估算，不是腾讯云账单，
且不包含系统盘、流量、折扣和其他费用；正式决策必须用控制台账单或 Usage API 对账。

报告写入 `artifacts/evals/<run-id>/evaluation-report.json` 和
`evaluation-report.md`。`passed` 表示结果符合用例预期；例如预期超时的用例
实际返回 `EXECUTION_TIMEOUT`，仍然算测试通过。`cleanup_rate` 只统计已经创建
实例的用例，不能把没有创建成功误认为清理成功。

可对报告中的逐实例回执和 SHA256 做自动审计：

```bash
uv run python scripts/audit_evaluation_report.py \
  artifacts/evals/<run-id>/evaluation-report.json
```

当前评测器已支持串行、多轮、受控并发、成本估算和 95% Wilson 区间。
PPIO Provider 使用官方 Python SDK；真实同场对照需要单独的 PPIO API Key，不能
把 AGSX 的凭据或报告冒充为 PPIO 结果。

PPIO Code 对照（配置凭据后执行）：

```bash
export PPIO_API_KEY=你的_PPIO_API_KEY
export PPIO_DOMAIN=sandbox.ppio.cn
export PPIO_TEMPLATE=code-interpreter-v1
export PPIO_BROWSER_TEMPLATE=browser-chromium
uv run python -m evals.runner --provider ppio-sandbox --suite code-sales --concurrency 3
```

PPIO Browser 对照：

```bash
uv run python -m evals.runner --provider ppio-sandbox --suite browser --concurrency 3
```

`browser-chromium` 的 CDP、正文、截图、Artifact、Receipt 与清理已完成固定矩阵
实测；这不代表生产网络隔离或不同真实网站兼容性已经证明。

Browser DNS rebinding 需要受控 `rbndr.us` 主机和内网测试服务；执行入口为
`scripts/agsx_browser_dns_rebinding_poc.py`，结果为 `MANUAL_REVIEW_REQUIRED`
时不能当作安全通过。

两份同口径报告生成比较表：

```bash
uv run python -m evals.compare \
  artifacts/evals/<agsx-run>/evaluation-report.json \
  artifacts/evals/<ppio-run>/evaluation-report.json
```
