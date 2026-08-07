# PPIO 同场对照运行指南

本项目已经有基于 PPIO 官方 Python SDK 的 `PPIOSandboxProvider`。真实 PPIO 结果必须使用独立凭据；不能把腾讯 AGSX 的 `E2B_API_KEY` 直接当成 PPIO 凭据。

## 1. 配置

在当前终端临时设置，避免写入 Git：

```bash
export PPIO_API_KEY=你的_PPIO_API_KEY
export PPIO_DOMAIN=sandbox.ppio.cn
export PPIO_TEMPLATE=code-interpreter-v1
export PPIO_BROWSER_TEMPLATE=browser-chromium
```

也可以按 PPIO 的 E2B 兼容文档设置：

```bash
export E2B_API_KEY=你的_PPIO_API_KEY
export E2B_DOMAIN=sandbox.ppio.cn
```

项目会识别第二种写法，但只有当 `E2B_DOMAIN` 明确为 `sandbox.ppio.cn` 时才会复用 `E2B_API_KEY`，不会误用腾讯域名下的密钥。

## 2. 先跑 Code + 销售

先执行 Code+销售对照：

```bash
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run python -m evals.runner \
  --provider ppio-sandbox --suite code-sales \
  --concurrency 3 --cpu-cores 2 --memory-gib 4 \
  --cpu-price-cny-per-core-second 0.00003 \
  --memory-price-cny-per-gib-second 0.000015
```

报告会写入 `artifacts/evals/<run-id>/`。为了和 AGSX 成本可比，必须使用相同的 vCPU、内存、并发、重复次数和成功定义。

## 3. 生成对比表

```bash
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run python -m evals.compare \
  artifacts/evals/<agsx-report>/evaluation-report.json \
  artifacts/evals/<ppio-report>/evaluation-report.json
```

两份报告必须是同一个 suite、重复次数和并发配置；比较器只做描述性差异，不自动宣称某个 Provider 生产可用。

## 4. Browser 对照

官方 `browser-chromium` 模板通过 9223 端口提供 CDP；项目使用实例级 access token 连接：

```bash
export PPIO_BROWSER_TEMPLATE=browser-chromium
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run python -m evals.runner \
  --provider ppio-sandbox --suite browser --concurrency 3 \
  --cpu-cores 2 --memory-gib 4 \
  --cpu-price-cny-per-core-second 0.00003 \
  --memory-price-cny-per-gib-second 0.000015
```

2026-08-06 实测结果：Code+销售 33/33、Browser 30/30 通过，清理 63/63，评测后残留实例为 0。报告分别位于
`artifacts/evals/eval-20260806T041022Z-875a6a3a/` 和
`artifacts/evals/eval-20260806T041115Z-65983985/`。这证明低风险 POC 闭环，不等于生产网络隔离或 SLA 已验证。
