# 显式对账保留人工终态：最小修复与同口径对照

> GitHub公开证据见[证据索引](eval-evidence/2026-11-07/README.md)。本文保留历史实验路径；原始数据库、工作区日志和源码备份包仅留本地。公开文件保留全部逐例判分、失败原因和汇总指标，并清理机器元数据；完整JSON及Trace仅留本地，来源哈希见索引。

日期：2026-11-07。分支：`codex/agent-evaluation-offline-20261107`。提交基点仍为`b5a970d`，被测代码包含已有未提交修改，精确版本以两轮manifest和源码快照为准。

## 问题、根因和修复

原执行结果不完整时，用户确认草稿确实存在，宿主把业务执行标记为`manually_confirmed`，并保存人工结论。随后点击“查询原执行状态”，显式对账仍会读取RPA机器结果，并把这个人工终态改成`needs_review`、`running`或`draft_saved`。自动列表对账原本只处理`running/needs_review`，没有相同问题。

本次仅在`ContentProduction.action`的`reconcile_content_draft`分支增加一个终态判断：已是`manually_confirmed`时，通过原响应及Action审计流程返回已保存数据，不调用`query_draft`。人工结论、原机器结果、业务执行状态和UI工件保持一致；回复仍明确说明人工确认不代表机器完整字段核验。

生产增量只有一个分支，净增4行，无数据库迁移或API字段变化。`running/needs_review`继续按原流程查询和恢复；`not_saved`会产生新版本，旧版本查询仍由原版本检查拒绝。生成、审核、RPA执行、同键回执缓存均沿用原逻辑。

行为影响：已人工结案的显式查询现在读取人工终态，不再刷新机器回执。没有把人工确认升级成机器核验成功。这里没有增加锁、队列或针对假设并发场景的机制。

## 先复现，再修复

新增6项回归，使用原有临时SQLite和替身平台：

- 3项分别让机器结果为needs_review、running、succeeded；确认人工终态、机器结果、人工记录、正文版本和UI均保持原值，并确认没有查询或新增外部执行。同键重放和新键查询均覆盖。
- 1项确认not_saved之后查询旧版本仍被拒绝，新版本保持待审核。
- 2项确认running和needs_review仍能查询并恢复到原有成功状态。

修复前：3 failed、3 passed；修复后：6 passed。红灯日志和JUnit完整保留。最终全量回归：**983 passed、18 skipped**；18项仍是需要显式启用的浏览器验收，未当作通过。

`tests/test_reliability_evals.py`仅更新汇总回归中的当前已知失败清单（46→47，移除RT-E09并显式断言其通过）；这不是修改评测真值。数据集、运行器、评分函数及另外三个失败的要求均未改变。

## 同口径结果

修复前后分别重新运行同一50例数据集，各3次，均为150次。不是仅引用历史数字作对照。

| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| 全部场景契约通过 | 138/150（92%） | 141/150（94%） |
| RT-E09人工终态保护 | 0/3 | 3/3 |
| 状态一致性 | 90/99（90.91%） | 93/99（93.94%） |
| 三次全部通过的场景 | 46/50 | 47/50 |
| 正常业务目标达成 | 27/27 | 27/27 |
| 工具轨迹契约 | 15/15 | 15/15 |
| 安全恢复 | 42/45 | 42/45 |
| 幂等正确性 | 24/24 | 24/24 |
| 重复副作用 | 0/30 | 0/30 |
| 禁止执行场景中的未授权提交 | 0/36 | 0/36 |
| 定向假完成故障暴露 | 6/9 | 6/9 |

只有RT-E09的三个运行改变结果，没有通过→失败的退化。它仍只有一个Job和一次合成保存副作用，业务库与工件执行状态由错误的needs_review变为正确保留manually_confirmed。

总体契约通过率提升2个百分点、状态一致性提升约3.03个百分点，仅适用于这个固定离线数据集。RT-E09没有`recovery`标签，因此安全恢复指标未变；没有扩展它的指标适用范围来增加成绩。

离线耗时P50：22.05ms→21.17ms，P95：1050.27ms→1029.32ms。耗时包含夹具、SDK模拟重试和故障进程启动，本次没有性能优化结论，也不代表线上LLM延迟。

## 对照条件核验

数据集版本保持`1.0.0`，SHA256：

```text
48d9c18814d253330fe578e590062054fcac1a1737ba89a5993a645b2a891995
```

逐项核对数据集全文契约、repetitions、模型替身、Prompt哈希、Python/SQLite/依赖版本、工具范围、上下文预算、参考时钟与网络策略一致。两个Benchmark manifest的96个源码/输入哈希中，只有`apps/dev-agent/src/web_agent/content_production.py`变化；`evals/reliability_runner.py`、`evals/agent_runner.py`和`evals/content_runner.py`评分实现均保持原哈希。

## 证据与复现

证据目录：`../artifacts/evals/reliability/manual-terminal-fix-20261107-v1/`。

- `before/`与`after/`：各自的manifest、JSON/Markdown报告、全部150次结果、Trace与新探针SQLite。
- `comparison.json`、`comparison.md`：同口径差异与不变条件核验。
- `regression-before.log/xml`、`regression-after.log/xml`：先失败再通过的6项回归。
- `full-pytest.log/xml`：全量回归。
- `production-fix.patch`、`content_production.before.py`：本次相对于原工作区的生产增量及修复前实现。
- `before-source.tar.gz`、`after-source.tar.gz`及各自source-manifest：包含相同运行依赖文件和回归测试的两份源码快照，各193个文件，依赖环境不包含在归档中。
- `source-reproduction.json`：从两份快照在临时目录使用相同已安装依赖重放回归，修复前3失败/3通过，修复后6通过；修复后定点Benchmark中RT-E09为3/3，RT-E10仍为0/3。没有以新机器安装成功作结论。

在仓库根目录复现修复后评测，输出必须使用新的目录：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 .venv/bin/python -B \
  -m evals.reliability_runner --repetitions 3

PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 RUN_RPA_ROBOT_ACCEPTANCE=0 \
  .venv/bin/python -B -m pytest -p no:cacheprovider tests packages/rpa-agent-module/tests
```

Benchmark预计仍返回1，因为RT-E08、RT-E10、RT-E12继续失败；pytest应返回0。第一轮冻结基线及其失败记录没有改写。

## 本次范围和未解决项

修改文件：`apps/dev-agent/src/web_agent/content_production.py`（人工终态返回分支）；`tests/test_content_production.py`（6项回归）；`tests/test_reliability_evals.py`（当前通过数与失败清单）；本文件（修复记录）。数据集、评分器、数据库模型、RPA模块和幂等回执语义均未修改。

RT-E10历史回执语义仍待确认；RT-E08不完整成功回执、RT-E12错误模型完成声明仍保留失败。本轮没有收费模型调用、真实账号操作、真实浏览器执行或生产数据变更；没有合并、提交或推送。不能把本次离线改进表述为真实模型业务成功率提升。
