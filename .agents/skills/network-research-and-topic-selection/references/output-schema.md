# 14.2 输出约束

`save_content_research_result` 是唯一落盘入口，其 Tool Schema 是机器可执行的最终约束。本文件补充字段语义。

- `company_name`、`company_profile`、`operation_goal`：保留用户本轮显式输入的含义，不用记忆或模型常识补齐。
- `queries`：本轮实际调用过的搜索词。
- `research_summary`：写作建议约 100–200 字，只概括结论；完整证据放在对应发现与来源中。此建议不是新增保存校验条件，旧长文本仍可读取。
- `key_findings`、`user_needs`、`competitor_findings`、`platform_findings`：每项用 `kind=fact|inference` 区分事实与判断。`fact` 必须有 `source_refs`；`uncertainty`不可留空，事实也要说明样本或核实范围，不能用空字符串占位。
- `source_references`：`source_id` 在工件内唯一；仅允许 `platform=xiaohongshu`；保存实际 URL、搜索词和 Tool 返回的 `collected_at`。
- `candidate_topics`：成功或部分成功时为 1-10 项（证据不足时缩减，不凑数）；`evidence.source_refs` 必须指向 `source_references.source_id`。
- `candidate_topics[].why_fit_company`：1-1000 字符的字符串，不是数组或对象；先按 Tool Schema 自查类型和长度。错误回执 `validation_errors` 包含具体字段路径、类型与要求，不包含原始错误输入。
- `ratings`：`trend`、`demand`、`business_fit`、`commercial_value`、`competition` 均使用 1-5 整数，并给出 `reason`。不计算总分。
- `recommendation`：仅为 `high`、`medium`、`low`，仍是建议，不等于已批准。
- `execution_status`：`success`、`partial_success`、`failed`。数据源不可用或字段缺失必须写入 `limitations`。
- `approval_status`：由保存 Tool 固定写为 `pending_confirmation`，14.2 无权自行批准。

## 14.1 来源与版本

- `goal_artifact_path` 是保存工具的可选输入：仅使用用户显式指定、已经读取并确认的目标版本。提供后可省略 `company_name`、`company_profile`、`operation_goal`，由工具从原文件注入；重复传入时必须一致。
- 新研究文件采用 `schema_version=1.1`。`input_mode` 区分 `goal_artifact` 与 `explicit_text`。
- `goal_ref` 保存目标文件路径、ID、版本和内容哈希；`goal_snapshot` 保存完整企业、结构化目标和确认记录。两者均由保存工具生成，模型不得自行填写。
- 显式文本入口的 `goal_ref`、`goal_snapshot` 为 null，不伪造上游确认。旧版研究读取时标记为 `legacy`。
- 14.3 读取时会核验引用与快照，并保留研究状态和局限；不会把部分成功升级为完整研究，也不会自行换成后来修改的目标。

组合目标快照中，`operation_goal` 为月目标背景、`weekly_goal` 为本轮研究服务的简单周目标。读取两者后以周目标确定研究重点；保存时省略 `operation_goal` 文本，由宿主注入周目标摘要并保留完整月、周快照及共同版本。

## 图片观察与推测

- `candidate_topics[].evidence` 的 `fact` 字段沿用名称，但用 `kind=fact|inference` 区分直接观察与推测；推测必须填写 `uncertainty`。旧工件省略 kind 时按原有事实字段兼容读取，不代表旧结论已经重新核实。
- `visual_analyses` 由宿主从本轮真实图片工具回执注入，模型保存时不要填写、复制或改写。包含成功/失败状态、笔记链接、实际图片序号、观察与不确定性。
- `candidate_topics[].visual_refs` 只能引用成功分析的 `analysis_id`；如省略但已填写visual_borrowing，宿主仅根据该选题所引用笔记的真实链接关联本轮成功图片回执，没有对应分析则仍拒绝保存。`evidence.source_refs` 可引用对应笔记来源，也可直接引用这个成功的分析编号；宿主会自动补入该编号对应的真实笔记链接，不需模型抄写视觉来源。`visual_borrowing` 是建议，不是图片事实或效果证明；没有视觉证据时留空，不能伪造分析。
- 图片分析仅覆盖明确列出的图片，不代表阅读整篇全部图片；视觉模型也可能误读，所需素材及最终业务事实仍需人工核实。

## 图片覆盖与照片总数

视觉回执可带available_image_count（宿主从详情取得，未知为null）。不能由模型填造；仅有图片链接不代表已看图。历史回执缺少总数时保持未知，不回写历史研究。

候选增加planned_photo_count（0至100的整数或null）。未明确照片上限时可省略；宿主明确本轮照片上限时必填，表示该候选实际需要的现有照片总数，required_materials不得与之冲突。检查失败返回具体字段和原因，修正原草稿，不重新取数。
