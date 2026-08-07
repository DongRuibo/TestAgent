# AGSX POC 前三周执行报告

- 执行日期：2026-08-05
- 项目：TestAgent / Lazyx Sandbox POC
- 当前结论：前三周均已完成；Browser 结论为 `GO_FOR_LOW_RISK_POC`
- 决策边界：当前结果只支持继续低风险 POC，不代表生产可用

## 第一周：Code POC 固化

真实 AGSX 执行结果为 `passed`：

| 检查 | 结果 | 证据 |
|---|---|---|
| Python | 通过 | 远端输出 `PYTHON_OK:10`，341 ms |
| JavaScript | 通过 | 远端输出 `JAVASCRIPT_OK:10`，330 ms |
| 文件上传/下载 | 通过 | 49 bytes，上传与下载 SHA256 一致 |
| SANDBOX 网络 | 通过单次探针 | DNS 可解析；到 `1.1.1.1:443` 的 TCP 连接超时，未连通公网 |
| 执行超时 | 通过 | 1 秒超时返回 `EXECUTION_TIMEOUT` |
| 实例销毁 | 通过 | `destroyed=true` |

归档报告：

```text
artifacts/code-poc-20260805T070513Z/code-poc-report.json
SHA256 b225e4768a3679b5c86188bbdb0db1efb466694da6a5a034bff9848f2dc01b77
```

注意：这只证明一次指定 TCP 探针未出网。DNS、HTTP/HTTPS、IPv4/IPv6、
Metadata、VPC 地址和多次重复样本仍需在第四周安全矩阵中扩大验证。

## 第二周：销售数据分析闭环

固定销售 CSV 已完成以下真实链路：

```text
输入归档 → 创建 AGSX Code Instance → 上传 CSV → 远端分析
→ 下载 JSON/SVG → SHA256 校验 → ExecutionReceipt → 销毁实例
```

分析结果：

| 指标 | 值 |
|---|---:|
| 数据行数 | 6 |
| 总销售额 | 800000.00 |
| 平均订单额 | 133333.33 |
| 1 月至 3 月增长 | 85.0% |
| 最高销售区域 | 华东 |
| 最高销售产品 | 专业版 |

产物：

```text
artifacts/sales-20260805T070531Z/summary.json
artifacts/sales-20260805T070531Z/sales-chart.svg
artifacts/sales-20260805T070531Z/execution-receipt.json
```

Receipt 状态为 `passed`，实例 `destroyed=true`。本轮使用本地 Artifact Store
证明协议和哈希闭环；对象存储、数据库状态机和不可变审计仍属于后续工程化。

## 第三周：Browser Go/No-Go

已完成：

- AGSX Browser Provider 创建与 CDP URL 封装。
- Playwright 公开网页标题、正文和 PNG 截图。
- Metadata、IPv4/IPv6 回环地址、私网地址和重定向探针。
- 两个独立 Browser Instance 的 Cookie 串扰测试。
- Prompt Injection 页面内容统一标记为 `untrusted`，且不可作为可执行指令。
- 所有已创建实例进入 `finally` 清理；模板创建失败时不启动本地 Playwright。

腾讯云已创建以下工具：

```text
ID：sdt-4aawzfxd
名称：lazyx-browser-public-poc
类型：浏览器沙箱
生命周期：5 分钟
网络策略：PUBLIC
CAM 角色：不配置
```

修正探针判定后，真实结果为 `passed`，决策为
`GO_FOR_LOW_RISK_POC`：

| 检查 | 结果 | 边界 |
|---|---|---|
| 公开网页标题、正文、截图 | 通过 | `example.com` 返回 200 |
| Prompt Injection 标记 | 通过 | 内容为 `untrusted`，`instructions_executable=false` |
| Metadata | 本轮无响应 | 4 秒超时，不等于已证明 Provider 原生阻断 |
| IPv4/IPv6 回环 | 本轮不可达 | 连接被拒绝 |
| 私网地址 | 本轮无响应 | 4 秒超时 |
| 重定向到 Metadata | 未进入目标 | 最终未到达保留地址 |
| Cookie 跨实例隔离 | 通过 | 第二个实例未读到第一个实例的随机 Cookie |
| 实例销毁 | 通过 | 两个 Browser Instance 均 `destroyed=true` |
| DNS 重绑定 | 未测试 | 需要受控测试域名 |

通过报告：

```text
artifacts/browser-poc-20260805T080516Z/browser-poc-report.json
SHA256 04acb50a3d0df652e331fb20912ecf0f4a03a4b66d2c75f540168cfde943eb31
```

截图：

```text
artifacts/browser-poc-20260805T080516Z/example-homepage.png
SHA256 57127f13bb7a15e175a136382de5d27a2ebb59bc77a7efdbb8c5538ffd3ab48f
```

需要强调：本轮“不可达/超时”只说明当次没有取得 HTTP 响应，不能替代网络
策略证据；PUBLIC Tool 也不能误标为域名白名单。DNS 重绑定仍为 `not_tested`。

## 自动化与遗留资源检查

- 本地自动化：41 passed。
- Python 编译检查：通过。
- 腾讯云孤儿扫描：`[]`，当前没有带 `managed_by=lazyx-testagent` 标记的遗留实例。

## Agent Loop 集成演示

前三周 POC 完成后，销售分析和公开网页 Browser 已注册为 Agent Tool。当前
`main.py` 向模型提供：

```text
read_file
run_code
analyze_sales_csv
browse_public_page
```

真实销售 Agent 运行中，模型第一轮选择 `analyze_sales_csv`，第二轮基于工具
结果回答。产物位于：

```text
artifacts/agent-sales-20260805T090052Z-327b301b/summary.json
artifacts/agent-sales-20260805T090052Z-327b301b/sales-chart.svg
artifacts/agent-sales-20260805T090052Z-327b301b/execution-receipt.json
```

真实 Browser Agent 运行中，模型第一轮选择 `browse_public_page`，提取
`Example Domain` 后生成正文、截图和 Receipt：

```text
artifacts/agent-browser-20260805T090145Z-75d95067/page-content.txt
artifacts/agent-browser-20260805T090145Z-75d95067/page-screenshot.png
artifacts/agent-browser-20260805T090145Z-75d95067/execution-receipt.json
```

两个 Receipt 均为 `passed`、`cleanup.destroyed=true`，随后孤儿扫描仍为 `[]`。
销售文件限制在 `workspace/` 与 `fixtures/`；Browser 拒绝显式本机、私网和
Metadata URL，并把网页正文标记为不可信观察数据。

## 当前决策

- Code/File 路线：可以继续扩大 POC 样本，但尚未达到生产稳定性证据量。
- 销售分析 Agent：闭环已经成立，可以在 Agent Loop 中增加受控业务入口。
- Browser 路线：允许继续低敏、公开网页 POC；登录态、敏感数据和生产任务仍为 `NO_GO`。
- 生产接入：仍需第四周的重复样本、并发、失败注入、成本和 PPIO 对照。
