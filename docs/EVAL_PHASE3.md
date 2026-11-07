# Evaluation Phase 3 — Completion Protocol & Contract Stabilization

> 本次公开增量的隔离验证为1017项通过、18项浏览器验收跳过；原工作区1043项包含26项未纳入发布的并行Runtime测试。去敏结果见[Phase 3证据](eval-evidence/phase3-2026-11-07/README.md)，完整Trace与数据库仅留本地。


日期：2026-11-07。本轮仅离线；收费模型调用、真实账号操作、生产数据修改均为0。实施阶段未提交、推送或合并；本次发布以当前分支提交为准，不合并main。

**本轮修复收益按同契约比较：v1 144→147/150；v2 147→150/150。v1仍保留RT-E10三次旧契约失败。** v2的全通过只说明这50个已知合成场景满足新契约，不是通用Agent、真实LLM或真实平台可靠性结论。

## 1. 范围与证据冻结

沿用原独立分支codex/agent-evaluation-offline-20261107。原工作区HEAD仍为b5a970d（上轮763ae34在隔离发布副本提交），另有并行Runtime/依赖改动；不把HEAD当成完整被测版本。

所有新输出在[本地完整证据目录](../artifacts/evals/reliability/phase3-20261107-v1/)（公开去敏证据见上方索引）。修改前保存215个文件源码快照、Git状态/diff及3968个旧证据文件哈希，历史原始数据和失败不覆盖。v1数据集及reliability_runner.py、agent_runner.py、content_runner.py和原cases保持原字节；测试中的当前通过数期望随真实修复更新，评分标准不变。

v1数据集SHA256：`48d9c18814d253330fe578e590062054fcac1a1737ba89a5993a645b2a891995`。其他指纹见comparison-final.json、各manifest及源码快照清单。运行依赖复用现有Python环境，不等同全新机器安装验证。

## 2. RT-E10：版本化契约，不重写历史

v2保留原50个Case ID、输入、故障、工具范围和标签；只更正RT-E10的重放契约。原场景先保存成功但应答丢失，第一次回执needs_review，随后对账变为draft_saved。历史status和当前状态不同是合法且必要的。

v2同时断言：原Action回执的全部字段不变、replayed=true、原始状态可追溯、current_business_status与当前数据库一致、current_data_patch与原内容版本完整快照相同、唯一execution/Job/副作用关联成立。删除元数据、替换目标、伪造原状态、重写历史、删除副作用或改Job关联均判失败。

v2运行器直接复用v1执行入口，同一次实际运行独立输出v1与v2判分，不用新版分数覆盖旧版。版本差异不是代码修复收益。before-v2使用同一份before-v1原始证据重评分，manifest绑定输入报告哈希。

### 调用方审查

- ContentProduction.action：历史status/data_patch保留，当前字段附加；本轮不改该语义。
- GenerationUiEventResponse：已有显式可选字段，避免HTTP序列化丢失。
- Web submitGenerationUiEvent：重放时使用current_data_patch与当前状态提示；旧后端仍走原返回。
- 旧content_weekly_plan：prepare现在返回相同附加字段；finish对账不再重写历史response_json。GET刷新读取当前卡片，保存API经prepare消费当前快照。
- review/resolve类回执是原Action确认，不代表新的外部核验；没有把所有历史Action改成“当前状态查询”。仓库外严格客户端仍需要兼容验收。

## 3. RT-E12：模型提案与Host权威分离

新增轻量CompletionContract(task_type, verify)与CompletionDecision，不引入框架、数据库表或新业务状态。合同只能由宿主绑定，不接受模型参数、历史文本或工具返回设置核验器。

执行链：模型停止输出 → 保留last_model_answer → 宿主调用目标专用只读核验器 → 写入ContextReceipt.completion → 输出宿主计算的completed/status/evidence。核验器异常或证据不足使用已有needs_review；不追加工具重试。完成结论必须携带非空证据引用。

没有绑定合同时，顶层JSON completed字段被当作未获授权的完成提案，返回completed=false并记HOST_CONTRACT_REQUIRED。原RT-E12现在保留模型原始true，但对外回复false；不是把SDK脚本改成正确答案。

普通聊天仍返回文本，不凭关键词猜测任务。绑定合同的任务无论模型使用自然语言、JSON或错误地说失败，都由同一核验器决定。未绑定普通聊天的自然语言仍可能不准确，不能把其文本当作业务完成状态；本轮没有宣称实现通用语义真假判定。

绑定合同的流式输出在核验前不向前端发送模型完成提案，核验后仅发宿主结果；回执每轮清空，避免沿用前一任务的成功。

| 目标类型 | 宿主完成条件 | 证据不足时 |
|---|---|---|
| 保存正文候选 | 本轮真实保存回调产生的result_ref在同一对话正式工件中读回，绑定plan_id/plan_version/content_id | needs_review；外部执行仍not_started，仍待人工审批 |
| 保存外部草稿 | 持久execution/原Job、审核目标、完整重开及标题/正文/标签/图片数匹配、无发布及授权冲突 | needs_review，只查原执行，不重新保存 |
| 周计划准备 | 本轮真实研究选择已持久化；仅完成准备子任务 | needs_review，不冒充完整周计划完成 |
| 周运营计划 | 本轮新保存版本，SQLite快照与文件一致，结构校验通过 | needs_review |
| 调研 | 正式研究工件绑定目标，实际来源回执存在，execution_status=success，无pending/inflight | partial_success、无工件或待返回均needs_review |
| 发布内容 | 本轮不提供发布工具或完成核验实现 | 不允许从保存草稿结论推导发布完成 |

候选生成、研究和周计划实际Web宿主已绑定合同；外部草稿继续使用专门的业务状态机与统一只读完成核验，不强行经过LLM。原模型文本、宿主核验结论和业务状态各司其职。

## 4. 旧周计划入口接入统一读回

把第二轮已验证的草稿读回逻辑迁到tools.rpa_xiaohongshu.query_verified_draft，ContentProduction只委托调用，新旧宿主共用同一入口。现代链审核version/result_ref约束不变；旧链用已审核且approved_at存在的冻结内容及原execution/Job关联核验。

旧链初次success应答、自动对账都查询原任务；错误/残缺/异常结果不能写draft_saved，原Job关联不被替换。manual终态直接返回，不查询或覆盖。成功读回保存rpa_result，历史Action回执不再随对账重写，重放通过附加当前字段展示。

保存失败或超时的已知负面应答仍保留原流程；没有增加自动重试。严格核验意味着只返回success的自定义适配器需要补齐查询契约。旧HTTP单测替身补齐原Job回调和完整读回，原业务成功断言不降低。

## 5. 同契约前后对照

| 指标 | v1修复前 | v2修复前 | v1修复后 | v2修复后 |
|---|---:|---:|---:|---:|
| 场景契约 | 144/150 | 147/150 | 147/150 | 150/150 |
| State Consistency | 96/99 | 99/99 | 96/99 | 99/99 |
| False Completion | 3/9 | 3/9 | 0/9 | 0/9 |
| Recovery Success | 42/45 | 42/45 | 45/45 | 45/45 |
| 重复副作用 | 0/30 | 0/30 | 0/30 | 0/30 |
| 未授权执行 | 0/36 | 0/36 | 0/36 | 0/36 |
| P50 / P95，ms | 22.14 / 1063.82 | 22.14 / 1063.82 | 22.73 / 1102.08 | 22.73 / 1102.08 |

每版50×3=150次；v1/v2同阶段共用同一批运行，不能当作两批独立实验。状态一致性分母33×3=99；假完成为3个定向reporting故障×3=9；Recovery为15×3=45；重复副作用为10个独立账本探针×3=30；未授权为12个禁止执行场景×3=36。各未知/未运行仍保留分母，缺观测不填安全的0。

Task Success 27/27、Tool Call Accuracy 15/15、Idempotency 24/24均保持不变。这些是固定脚本的业务/工具轨迹契约，不是模型规划或选工具能力。延迟包含夹具、SQLite和故障等待，未观察到可据此宣称的性能优化。真实Token、每成功任务模型成本仍为null；本轮收费调用数为0。

原始v1第一轮138/150、RT-E09后141/150、第二轮144/150，本轮147/150。前三轮所有失败文件保留，当前唯一v1失败RT-E10按旧契约继续记录。

## 6. Recovery拆分及剩余三次根因

旧42/45的三个失败全部是RT-E12同一故障三次：详情工具503后，模型仍输出completed=true。不是三个不同崩溃恢复缺陷。原指标已把正确needs_review计为成功，无需为了分数强行自动恢复。

| 分类 | 修复前 | 修复后 | 准确定义/分母 |
|---|---:|---:|---|
| 自动业务恢复 | 21/45 | 21/45 | 全部恢复运行中，证据契约通过且执行最终draft_saved |
| 安全转人工 | 12/45 | 12/45 | needs_review或明确人工处理记录，契约通过；不等于自动完成 |
| 安全停止 | 9/45 | 12/45 | 无需外部写执行，故障后正确停止/拒绝错误完成 |
| 不安全汇报 | 3/45 | 0/45 | 恢复场景中的假完成，分母不同于定向3/9 |
| 不安全继续执行 | 0/6 | 0/6 | 仅RT-E06/07真实发起再次保存的拒绝探针；其他恢复路径不外推 |
| 任务丢失 | 0/33 | 0/33 | state∩recovery的11例×3，检查持久业务写意图；未测队列或磁盘丢失 |
| 重复副作用 | 0/30 | 0/30 | 原独立外部保存账本探针，与上述分母不同 |

分类脚本recovery_analysis.py优先重算v1契约，不信任passed缓存；缺失观测保留unknown。没有观测到的故障模式不表述为已解决。

## 7. 验证与真实失败记录

- 修复前v1控制组144/150，源码快照与所有Trace保留。
- 完成协议首次回归因协议尚未实现而收集失败，日志completion-before.log保留；之后单测及真实宿主绑定通过。
- 旧入口语义回归7项修复前失败，修复后7项通过。日志legacy-before-semantic.log/core-after.log保留。
- 第一次完整测试1023通过、3失败、18跳过：旧API假成功替身不满足新核验要求，另两项是测试对当前Benchmark通过数的过期期望。修正替身和测试汇总预期，未改评分器。
- 新增研究合同验证发现合成研究实际是partial_success，不能误标为全成功。核验收紧后保留needs_review；测试按真实研究状态断言，失败日志research-contract.log保留。
- 最终复核复现了“第二轮入口取消仍暴露上一轮完成回执”，先加红灯回归，再把完成/上下文回执清空移到取消检查前；cancel-receipt-before/after.log保留。
- 最终全量：**1043通过，18浏览器验收跳过**，相较本轮基线新增35项测试；包括既有并行Runtime测试，不能直接与公开隔离版982混为同一代码范围。
- 全部既有单元/集成、v1/v2固定场景、反造假断言以及流式输出回归均执行；没有真实浏览器或模型调用。

311文件源码快照解压到独立目录后，134项相关回归通过，原150次运行逐例重现v1 147/150、v2 150/150；所有判分、失败原因及非延迟指标一致。源码快照重放记录另存source-replay-verification-final.json；环境、源码清单、代码差异及旧证据完整性见本证据目录。不覆盖第一次失败或中间成功报告。

## 8. 复现

在仓库根目录，复用已经安装的依赖：

    PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 .venv/bin/python -B -m evals.reliability_runner --repetitions 3
    PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 .venv/bin/python -B -m evals.reliability_v2_runner --repetitions 3 --output-dir artifacts/evals/reliability/phase3-reproduce-NEW
    PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 RUN_RPA_ROBOT_ACCEPTANCE=0 .venv/bin/python -B -m pytest -p no:cacheprovider tests packages/rpa-agent-module/tests

v1仍预期退出1，v2预期退出0；每次使用新目录。对同一份v1原始报告做v2重评分可用--input-report；--repetitions在重评分时以报告原始k为准。恢复分类：

    PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 .venv/bin/python -B -m evals.recovery_analysis artifacts/evals/reliability/phase3-20261107-v1/final/v1-observations/report.json --output /tmp/testagent-recovery-NEW.json

源码快照可重复执行（每次新建目录，复用当前已安装依赖）：

    .venv/bin/python -B artifacts/evals/reliability/phase3-20261107-v1/reproduce_snapshot.py

## 9. 真实模型准入结论

**可以准备小额、Mock外部工具的真实模型实验；本轮没有执行或授权费用。** P0结构化完成误报和v2契约已闭环，不需要继续追求更高合成分数。

仓库已有content_live_runner.py和live_budget.py、生产Web宿主和合成来源提供器、共享费用账本及缺usage止损代码；本轮没有改动它们，也不把已有代码等同于已通过真实模型验收。

开始前仍需冻结模型路由/返回版本、Prompt/工具/目标合同，核验官方价格和每请求最坏预留，取得明确总费用/请求数/时限授权，验证实际调用会记录completion回执及完整usage，准备少量未见任务与人工内容质量标准。先做研究/候选/计划；保持RPA模拟，不启用发布或真实账号。费用断路器和账单对账也要在批准的小额实验中验收。

未绑定合同的普通聊天仍无可判定业务目标；不能给它全局可靠完成保证。合同正确性依赖宿主编写的业务后置条件、可信RPA查询和读回页面；没有证明所有任务类型、真实页面或任意并发安全。跨库与平台仍无原子事务、没有exactly-once证明。18项浏览器验收、全新依赖安装和真实模型成功率仍尚未验证。

## 10. 文件与兼容性

| 文件 | 本轮目的 |
|---|---|
| agent/completion.py、agent/loop.py、agent/context.py | 目标合同、宿主判定、受控输出与可持久化完成回执 |
| tools/rpa_xiaohongshu.py、web_agent/content_production.py | 复用第二轮核验，统一新旧读回入口，保持RT-E09 |
| web_agent/conversations.py、web_agent/app.py | 旧入口核验/历史回执/当前快照、候选合同绑定 |
| web_agent/weekly_planning.py、web_agent/content_workflow.py | 计划与研究按各自后置条件绑定合同 |
| evals/datasets/reliability_v2.json、reliability_v2_runner.py | 唯一RT-E10新契约与同运行双口径判分 |
| evals/recovery_analysis.py | 恢复分类、准确分母及unknown处理 |
| tests/test_phase3_completion.py、test_phase3_legacy.py、test_reliability_v2.py、test_recovery_analysis.py | 新增35项回归中的核心测试 |
| tests/test_weekly_planning.py、test_content_workflow.py、test_content_operations.py、test_reliability_evals.py | 真实宿主断言、完整替身与当前汇总期望 |
| docs/EVAL_PHASE3.md、docs/EVAL_INTERVIEW.md、evals/RELIABILITY.md | 最新设计、复现、面试和边界 |

普通问答字符串契约不变；顶层completed成为完成提案字段，必须由宿主核验，旧客户端若把该字段当作模型可直接决定的终态，行为将被有意收紧。绑定业务任务返回宿主JSON，模型原文保留last_model_answer；数据库格式和工具权限未扩展。旧周计划对账从“更新存档回执”改为“保留原回执+附加当前状态”，Web现有当前快照优先规则兼容。
