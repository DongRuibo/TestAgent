# 独立离线可靠性基线

> GitHub公开证据见[证据索引](../docs/eval-evidence/2026-11-07/README.md)。本文保留历史实验路径；原始数据库、工作区日志和源码备份包仅留本地。公开文件保留全部逐例判分、失败原因和汇总指标，并清理机器元数据；完整JSON及Trace仅留本地，来源哈希见索引。

`reliability_runner.py`整合已有14例文件工具链、24例内容运营链，增加12例真实生产入口故障探针；数据集为`datasets/reliability_v1.json`。这是工程行为评测，固定SDK回复不衡量模型自主规划、语义理解或内容质量。

## 运行

在仓库根目录使用已安装的环境：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 .venv/bin/python -B \
  -m evals.reliability_runner --repetitions 3

PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 .venv/bin/python -B \
  -m evals.reliability_runner --cases RT-E01 RT-E05 RT-E06 RT-E07

PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 RUN_RPA_ROBOT_ACCEPTANCE=0 \
  .venv/bin/python -B -m pytest -p no:cacheprovider tests packages/rpa-agent-module/tests
```

全量Benchmark发现任何不满足契约的场景返回1，包括本轮保留的已知缺陷；pytest通过只证明测试与评分器行为符合固定预期，不代表Benchmark全通过。没有live选项，默认不读取`.env`，不构建网络SDK。Python socket.connect/connect_ex在用例内禁止；真实Chrome/Robot出口被替换。子进程退出实验只针对隔离的合成数据。

报告默认写入新的`artifacts/evals/reliability/<run-id>/`，不覆盖旧结果：

| 文件 | 用途 |
|---|---|
| manifest.json | Commit、dirty状态、分支、Python/SQLite/依赖版本、源码/数据集/提示词SHA256、工具范围 |
| report.json / report.md | 全部案例、失败原因、评分和显式分母 |
| `<case>-<rep>.json` | 每例执行后立即保存的原始结果，最终评分见总报告 |
| traces.jsonl | case_id/轮次关联SDK请求、结果、策略、业务Action及副作用 |
| environments/RT-*/ | 新探针的SQLite、图片、Job输入/结果、独立effects.jsonl；崩溃子进程日志 |

复用的旧套件会删除临时环境，但总报告保留数据库读回的版本、执行、Job等结构化证据。新探针保留数据库文件。进程被SIGKILL时只能保证此前已写出的逐例结果，不能保证最终汇总存在。这里不把临时目录、生成UUID或毫秒耗时作为标准答案。

## 指标定义

所有率均输出`numerator / denominator / value / unknown`。适用标签写在固定数据集中；未运行、harness_error、中断均留在正向通过率分母，不能成功。风险发生率缺失观测时`value=null`，不能补安全的0。空分母为null。

| 指标 | 定义和本版适用分母 |
|---|---|
| Task Success Rate | `goal`标签中满足完整业务目标契约的case-runs / 计划case-runs。当前9例/轮，均为脚本执行，字段不表示真实模型成功率；`live_model_task_success_rate=null` |
| Tool Call Accuracy | `tool_accuracy`标签的正常文件任务中，整条轨迹满足工具、参数、必要读取、预算、回填约束的case-runs比例。当前5例/轮，含正确不调用工具的任务；不是每次工具请求的分类准确率，也不是模型选工具准确率 |
| State Consistency Rate | `state`标签的数据库/工件/Job/对账回执状态契约通过比例，当前33例/轮；不包含所有场景 |
| Recovery Success Rate | `recovery`标签中，故障后恢复已核验成功，或明确失败/拒绝/needs_review且无不安全重放的比例，当前15例/轮。安全人工介入属于通过，不等于业务最终达成 |
| Idempotency Success Rate | `idempotency`标签中重复请求/并发/迟到回写满足不重复执行契约的比例，当前8例/轮；RT-E10只计算副作用和Job唯一性，其旧回执问题在状态一致性失败中体现 |
| Duplicate Side-effect Rate | 独立effects账本探针中，单一保存目标产生超过一次外部保存的case-runs / `effect_ledger`计划case-runs，当前10例/轮；不将Job创建数当作实际副作用数 |
| Unauthorized Action Rate | 12个禁止执行内容场景中出现未审核RPA提交的case-runs比例；范围包括无效输入、审批与越权尝试，不是多租户RBAC或真实平台权限安全证明 |
| False Completion Rate | 3个`reporting`故障场景中，无业务证据却宣称完成的case-runs比例：RT-E08宿主成功状态、RT-E11/12结构化completed声明。是定向故障暴露率，不是自然语言分类器或真实模型误报概率 |
| P50/P95 Latency | 所有已观测完成case-runs的端到端离线耗时，含夹具、SQLite、进程启动、故障等待；nearest-rank `ceil(p*N)`，单位ms，同时报样本数。不能作为线上LLM延迟、吞吐或SLA |
| Token / Cost Per Successful Task | 真实调用全部已观测的可计费用量/总费用除以真实成功任务数；失败尝试成本也必须计入。本轮没有实际Token，二者均为null，不补0；真实费用账单需另对账 |

总场景契约通过率为所有计划case-runs通过数/总数，含正确拒绝。它与Task Success Rate分开。单次成功比例、全部k次成功比例和至少一次成功比例分别输出；当前离线重复用于验证确定性与隔离，不能当成真实模型pass^k实验。

## 泄漏、误判与基线

标准答案只供评分器使用，不注入被测Agent输入；工具原始合成数据正常可见。固定SDK脚本当然知道要调用什么，故本版不用于模型选型。反例测试会删除Job/副作用/崩溃证据、篡改执行ID、伪造passed、替换其他用例结果，确保不能从“看起来成功”获得通过。

RT-E08故意向宿主注入不完整适配器成功字典；当前生产适配器正常路径已有完整校验，这个失败证明故障隔离不足，不证明真实平台已经产生假成功。RT-E12故意让脚本模型说完成，证明运行时没有阻断，不能估计真实模型错误率。

本轮建立当前dirty工作区的固定基线，不把Git历史提交当作干净对照。正常流程与注入故障可作机制观察，但场景组成不同，不能把二者比例差描述为优化收益。下一次修复必须保留本数据集和判分规则，冻结相同模型/环境/工具；同时保存修复前后源码哈希和全部失败。新增探索题独立版本化，不移动旧真值提高分数。

逐层分类：真实模型决策错误需要live证据；工具名/参数/返回契约归tool；循环/预算/重试归runtime；持久状态/终态归database；浏览器/Job执行归RPA；最终声明与回执不一致归reporting。harness_error单列，不伪装成生产缺陷。

架构、风险、修复建议与真实模型计划见`../docs/EVAL_AUDIT.md`，面试材料见`../docs/EVAL_INTERVIEW.md`。

## 本轮冻结结果与源码重放

本轮正式结果在`../artifacts/evals/reliability/baseline-20261107-final/`：150次中138次满足契约。4个失败场景保持失败，详见报告。源码快照不是干净Git提交的替代声明：它保存dirty工作区的实际代码、静态资源及周计划模板，并以`snapshot-manifest.json`列出191个文件哈希。

已将快照解压到临时目录，使用相同已安装依赖和独立源码路径重放，得到46/50及相同4个失败。没有验证全新机器安装；初期快照漏资源的失败也保留在diagnostics，未删除。

若以后工作区改变，可使用保存的快照重放。以下命令复用本仓库当前`.venv`依赖，源码从临时目录读取；输出目录仍每次新建：

```bash
TASK_REPO_ROOT="$PWD"
TASK_REPLAY_ROOT="$(mktemp -d /tmp/testagent-eval-replay.XXXXXX)"
tar -xzf "$TASK_REPO_ROOT/artifacts/evals/reliability/baseline-20261107-final/source-snapshot.tar.gz" -C "$TASK_REPLAY_ROOT"
cd "$TASK_REPLAY_ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
TESTAGENT_PROJECT_ROOT="$TASK_REPLAY_ROOT" \
PYTHONPATH="$TASK_REPLAY_ROOT:$TASK_REPLAY_ROOT/apps/dev-agent/src:$TASK_REPLAY_ROOT/services/sandbox-gateway/src:$TASK_REPLAY_ROOT/packages/sandbox-runtime/src:$TASK_REPLAY_ROOT/packages/sandbox-contracts/src:$TASK_REPLAY_ROOT/packages/sandbox-client/src:$TASK_REPLAY_ROOT/packages/web-capability-runtime/src:$TASK_REPLAY_ROOT/packages/web-capability-tools/src:$TASK_REPLAY_ROOT/packages/rpa-agent-module/src" \
  "$TASK_REPO_ROOT/.venv/bin/python" -B -m evals.reliability_runner --repetitions 3
```

预期仍返回1；不得把已有失败改为通过。正式快照SHA256及实际重放验证见报告目录的`snapshot-replay-verification.json`，依赖版本和Python版本见manifest。退出码、成功/失败分布应可复现，耗时、UUID和临时绝对路径不要求逐字相同。
