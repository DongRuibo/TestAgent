# TestAgent 宿主映射

仅在 TestAgent 或明确暴露下列工具的宿主使用。本文件不是通用 Skill 的执行依赖；其他 Agent 可直接提供业务数据并使用自己的保存工具。

## 三个工具

1. `prepare_promotion_plan`：未传或传 null 的 `metrics_artifact_path` 时，内部调用一次 `collect_xiaohongshu_metrics()`：默认筛选最近30天首发笔记、最多保留20篇，采集成功后直接冻结输入并返回 `input_artifact_path`。不另行调用采集工具，也不要求用户复制路径。用户明确指定已有文件时才传该路径并跳过采集。可选 `goal_artifact_path`、`research_artifact_path` 用于复用已有资料，目标须为已确认的最新版本。
2. `load_promotion_plan`：传入 `artifact_path` 和 `section`。`overview` 看业务约束；`goal` 看目标资源与限制；`posts`、`research` 看证据；`candidates` 读前版计划。按 `next_offset` 继续，缺省每页最多 5 项。`json_fragment` 按 `record_index`、`part` 读全并拼接，不能把单个片段当完整记录。
3. `save_promotion_plan`：传入冻结输入路径、方案概述、假设和候选。返回 `report_path`、`artifact_path`、待补充项和版本。只有成功回执才能说明已保存。

`prepare_promotion_plan` 的业务字段：`company_name`、`company_profile`、`objective`、`primary_metric`、`target_audience`、`regions`、`start_date`、`end_date`、`budget_cap_cny`、`budget_basis`。未知字段留空；不要把模型建议的上限写成用户提供的上限。主要指标按工具 schema 中的枚举映射。

准备成功后，先读取需要的证据，再保存。允许在一次模型响应中请求多个相互独立的读取工具以减少轮数；保留轮数给保存和交付。资料缺失时可以保存待补充草案，不绕过校验。未指定数据的新建任务应先调用准备工具，缺企业资料、预算等也可留空生成待补充草案，不先追问文件路径。准备每轮最多一次：失败时保留 error_type、needs_user_action 和提示，停止本轮，不重试、不扫描旧文件，不调用保存伪造最新方案。首次登录、验证码或绑定不匹配由用户处理；用户明确重试后在新一轮调用。采集返回空帖子时说明当前筛选无数据，不编造候选。返回 collection.mode=fresh/existing 区分新采集与已有文件，展示 collected_at、post_count、source_total 及 truncated，不能把样本冒充全账号。

## 保存字段

可选 `planning_suggestions`：把人群、地区、周期或预算的建议值及依据写在这里，程序单独显示为待确认建议，不覆盖输入约束。未知预算上限时可在这里讨论建议范围，候选 `budget_cny` 保持 null。

顶层必填：`input_artifact_path`、`summary`、`assumptions`、`candidates`。至少一个候选；无有效笔记时说明无法生成并请求数据，不伪造索引。

保存参数保持紧凑：`summary` 用 2–3 句概括选择、分配与关键缺口；`assumptions` 仅列共用假设。完整业务约束与基线由程序写入报告，无需在概述、假设和每篇候选中反复抄写。每篇理由、假设与预期各用 1–2 句；共用经验来源链接可集中写入 assumptions 避免重复长URL；停止和追加各保留必要的 1–2 条规则，写清检查时点与所需数据。一次保存的说明文字尽量控制在 2500 个汉字内，避免长输出截断参数。`candidates` 必须是 JSON 数组，不能把数组再编码成字符串。

已有快照的统计周期未知时，不用历史累计观看给未来窗口增量设效果门槛。摘要尽量只列建议方向与预算，不重新压缩每篇的不同条件为统一比例。

缺少 spend 表示未取得花费数据，不能改写成从未投放或没有花费历史。未知流量口径下，不将曝光到观看的算式写成确定关系；假设也应是待检验的问题，不是带“若”字的因果断言。

每个候选：

- `recommendation`：`prioritize` 优先、`test` 小额试投、`optimize_first` 先优化、`hold` 暂不投；默认 test 兼容旧调用。后两类预算必须为 null，停止/追加列表可为空；不要为通过校验硬凑投流候选。
- `post_index`：本次 `posts` 中的零起始索引；由工具返回，不是平台 ID。
- `post_url`：没有已提供链接就留 `null`；只接受无查询参数的 `https://www.xiaohongshu.com/explore/<24位ID>` 或对应 discovery/item 链接。仍待人工核对与后台记录的关联。
- `reason`、`hypothesis`：用数据事实、适用经验 E编号/来源、建议方向和限制说明判断；不必把策略写成因果实验。经验来源写在 reason，不能放进研究 source_id。先优化/暂不投时说明重新考虑的前提。
- `research_source_refs`：仅使用研究里已存在的 `source_id`；无研究时为空，不伪造引用。
- `budget_cny`：建议分配金额，最多两位小数；未知上限时此项也应留 `null`。分配理由写入 `reason` 或 `summary`，保留额度不代表追加批准。
- `expected_result`：只传 `description` 和 `basis`，不要传程序生成的 `kind`。
- `stop_conditions`、`additional_budget_conditions`：各项只传 `condition` 与 `required_data`。所需数据名以工具枚举为准；检查时点、预算/时间边界及阈值依据写在 `condition` 中。程序填写缺失数据及未评估状态。

不要传 `baseline`、`approval_status`、`readiness`、`plan_id`、`version` 或计算汇总；这些由程序读取真实输入、校验和生成。

提交前对照结构化字段核对所有说明：正文中的候选金额必须与对应 `budget_cny` 一致，尽量不在其他候选的预期中重复金额。自行提出的时间、样本量、告警比例和观看时长阈值都必须标为待负责人确认的测试假设并说明依据，不能写成已验证标准；依据不足就保留待定。

修订旧方案时，先读取指定 plan.json 的 overview/posts/candidates 并复用其中冻结证据。其 input_snapshot 的来源仍由宿主保留；沿用该快照路径提交修订（overview 返回的 input_artifact_path），无需再次准备或采集。若用户要求刷新数据，则省略 metrics_artifact_path 重新准备；若业务约束变化而数据复用，显式传入 overview 的 source_refs.metrics.path，原文件不存在时说明需恢复或重新采集，不静默刷新。

修订须完整传入新建议，并增加 `previous_plan_artifact_path` 与 `revision_reason`。约束或数据变化时先准备新的输入，仍保持同一企业和账号。失败后按明确错误修正本地参数；不要重做付费取数，也不要反复创建重复计划。

## 数据与输出

官方后台采集当前提供曝光、观看、封面点击率、观看时长、互动与涨粉；咨询、有效线索、花费和笔记 ID 可能缺失。缺失不会补零。日期通常表示首发日期，不能说成最近一段时间新增的自然流量。

三工具仅提供数据与校验；选择和建议由模型分析。报告固定待审批，保存成功不代表有权人审批成功。交付给用户简短摘要，并使用工具返回的真实报告/JSON 路径；不要把完整内部工件直接贴满对话。
