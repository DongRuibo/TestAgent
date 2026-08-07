# AGSX 第四周评测与决策报告

日期：2026-08-06

## 结论

当前结论是：**AGSX 可以进入低风险 POC、内部演示和销售分析试用；暂不批准为生产默认 Provider。**

原因不是功能闭环未完成，而是生产决策还缺三类证据：

1. Browser 的 DNS rebinding 只完成导航前和重定向后 DNS 预检，云端解析器的 TOCTOU 行为尚未证明。
2. 成本目前是按生命周期秒数和公开 CPU/内存单价计算的估算，仍需分别用腾讯云与 PPIO 账单对账。
3. 当前矩阵适合 POC 描述性比较，尚未覆盖数百次稳定性、不同真实网站、故障注入和企业治理门槛。

## 已完成证据

最终合并报告：`artifacts/evals/eval-20260806T032636Z-6d1fdc70/evaluation-report.json`

Code 30、销售 3、Browser 30 共 63 个实例全部通过预期，清理 63/63，P50/P95 为
1,999/3,411 ms，观测最大并发 3，生命周期 149 秒，CPU+内存成本估算 0.039038
元，每成功任务估算 0.000651 元。这个合并数字用于总览，分项性能仍以各自报告为准。

跨租户隔离、销毁后残留和超时清理已真实通过：报告为
`artifacts/isolation-poc-20260806T032221Z-4e59a0ed/isolation-poc-report.json`。
两个不同 tenant/user 在相同远端路径读到各自标记，Metadata 绑定通过，超时分类为
`EXECUTION_TIMEOUT`，销毁后三个远端文件均不可读取，3/3 实例销毁；随后清理扫描为 `[]`。

### AGSX Code / Browser 扩大样本基线

Code 报告：`artifacts/evals/eval-20260806T032606Z-a1a0b0f2/evaluation-report.json`

Browser 报告：`artifacts/evals/eval-20260806T031058Z-1d8c4e00/evaluation-report.json`

| 指标 | Code 30 个固定任务 | Browser 30 个固定 URL |
|---|---:|---:|
| Case pass rate | 100%（30/30） | 100%（30/30） |
| 预期成功任务 | 29/29 | 30/30 |
| Cleanup | 30/30，100% | 30/30，100% |
| P50 / P95 | 856 / 2,552 ms | 3,110 / 4,450 ms |
| 观测最大并发 | 3 | 3 |
| 生命周期秒数估算 | 47 秒 | 110 秒 |
| CPU + 内存成本估算 | 0.012314 元 | 0.028820 元 |
| 每成功任务成本估算 | 0.000425 元 | 0.000961 元 |

Browser 的 30 个固定 URL 是 `example.com` 同源的 200/404 矩阵，适合验证重复导航、响应状态和清理；不应解读为 30 个不同网站的兼容性证明。

成本估算使用 2 vCPU、4 GiB，并以腾讯云计费页的 CPU/内存按秒单价为输入；不含系统盘、流量、折扣和实际账单差异。最终应以[腾讯云 Agent Runtime 计费说明](https://cloud.tencent.com/document/product/1814/133249)和账户账单对账。

评测器现在还输出 case、task success 和 cleanup 的 95% Wilson 区间，避免把 30 个样本误读成生产 SLA。

### Browser Sandbox

报告：`artifacts/browser-poc-20260806T030253Z/browser-poc-report.json`

- 公开页面标题、正文和截图：通过。
- Metadata、IPv4/IPv6 回环、私网地址和重定向：本轮均未到达目标；证据等级仍是“观测不可达”，不是 Provider 策略证明。
- Cookie 跨两个新实例不串扰：通过。
- 页面内容标记为 `untrusted`，不会自动执行页面指令：通过。
- 导航前 DNS 预检：通过；当前开发环境将公网 DNS 映射到 `198.18.0.0/15` 代理网段，因此该地址只作为本地 DNS 代理观测记录，不能代替云端解析策略。
- DNS rebinding：`inconclusive`，已执行受控实验但没有观测到连接地址，不能宣称已防护。
- 两个 Browser 实例均销毁；随后 `scripts/agsx_cleanup.py` 扫描结果为 `[]`。

### 测试与代码质量

- 离线测试：59 passed。
- 评测回执审计：`scripts/audit_evaluation_report.py` 检查最终 63/63 回执、文件哈希、
  清理状态和 sandbox ID，结果 `passed: true`。
- `run_code` Agent 工具现在也写入执行回执；成功样例
  `artifacts/agent-code-20260806T032356Z-ff3bc385/execution-receipt.json`，超时失败样例
  `artifacts/agent-code-20260806T032421Z-df123ca7/execution-receipt.json`，两次均销毁成功。
- 已实现并实测 `PPIOSandboxProvider`，业务层复用内部 Provider 协议，云端调用使用
  `ppio-sandbox` 官方 Python SDK；腾讯与 PPIO 凭据保持隔离。
- PPIO 评测后按 `managed_by=lazyx-testagent` 查询，残留实例为 0。

## PPIO 同场对照结果

完整运行说明见：[PPIO 同场对照运行指南](PPIO_同场对照运行指南.md)。

Code+销售 PPIO 报告：
`artifacts/evals/eval-20260806T041022Z-875a6a3a/evaluation-report.json`

Code+销售对比：
`artifacts/evals/compare-agsx-ppio-20260806T0410Z/comparison-20260806T041042Z-7413d102/comparison.md`

| Code+销售指标 | AGSX | PPIO |
|---|---:|---:|
| Case / Task / Cleanup | 100% / 100% / 100% | 100% / 100% / 100% |
| P50 / P95 | 814 / 2,244 ms | 791 / 1,904 ms |
| 每成功任务成本估算 | 0.000445 元 | 0.000168 元 |

Browser PPIO 报告：
`artifacts/evals/eval-20260806T041115Z-65983985/evaluation-report.json`

Browser 对比：
`artifacts/evals/compare-browser-agsx-ppio-20260806T0412Z/comparison-20260806T041202Z-ee7e058a/comparison.md`

| Browser 指标 | AGSX | PPIO |
|---|---:|---:|
| Case / Task / Cleanup | 100% / 100% / 100% | 100% / 100% / 100% |
| P50 / P95 | 3,110 / 4,450 ms | 3,427 / 4,632 ms |
| 每成功任务成本估算 | 0.000961 元 | 0.000500 元 |

这轮固定样本中，PPIO 的 Code+销售更快且估算成本更低；Browser 中 AGSX 略快，
PPIO 估算成本更低。两边成功率与清理率均为 100%，差异只是描述性结果，不是统计
显著性或生产 SLA 证明。PPIO Browser 已验证 CDP/Playwright、正文、截图、Artifact、
Receipt 和销毁，但没有证明生产级网络隔离。

DNS rebinding 的执行入口为 `scripts/agsx_browser_dns_rebinding_poc.py`。本轮使用
`7f000001.08080808.rbndr.us`（127.0.0.1 ↔ 8.8.8.8）做了 4 次云端 Browser
导航，4 次均超时、没有观测到 `server_addr`，结果为 `inconclusive`；实例销毁成功，
随后清理扫描为 `[]`。报告：
`artifacts/browser-dns-rebinding-20260806T031810Z-eedc0336/dns-rebinding-report.json`。
这不是安全通过，仍需要受控公网服务和内网测试服务才能证明 TOCTOU 防护。

自动创建两端服务的入口为 `scripts/agsx_dns_rebinding_controlled_poc.py`；当前
网络环境对 `rbndr.us` 的访问可能被代理，因此即使清理成功，结果仍可能是
`inconclusive`。

本轮自动两端实验报告：
`artifacts/dns-controlled-20260806T033221Z-f1c2df5c/controlled-dns-rebinding-report.json`。
Code 公网服务和 Browser 本地服务均启动成功，4 次导航均收到连接拒绝、没有
`server_addr`，两端 2/2 销毁且清理扫描为 `[]`；因此仍不能把 DNS rebinding 判为通过。

## Go / No-Go

| 范围 | 决策 | 约束 |
|---|---|---|
| 低风险公开网页、销售 CSV、代码解释器 POC | **GO** | 单次任务、低敏数据、TTL、强制 Receipt 和销毁 |
| 内部演示和开发联调 | **条件 GO** | 不放入真实客户密钥，不执行付款/发信/后台写操作 |
| 生产默认 Provider | **NO-GO（当前）** | 完成 DNS rebinding 专项、双边账单对账、扩大样本和策略审计后再复评 |

## 后续必做

1. 准备受控内网 HTTP 服务，在云端 Browser 内验证 DNS rebinding 的解析变化、最终连接目标和重定向路径。
2. 将评测估算与腾讯云、PPIO 实际账单、重试和流量费用对齐，计算每成功任务真实成本。
3. 扩展到数百次 Code 调用和多站点 Browser 样本，并加入超时、网络、浏览器崩溃和 Kill 失败注入。
4. 在进入 MVP 前补 Tool Policy、Approval、凭据代理、审计告警和多租户串扰测试。
