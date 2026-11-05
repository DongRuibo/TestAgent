# 运营目标结构契约

仅在以下场景读取本文件：

- 用户明确要求 JSON 或 YAML；
- 需要把目标交给其他 Agent 或系统；
- 用户要求测试或评估本 Skill；
- 需要明确字段或状态语义。

这是可移植的概念契约，不要求宿主 Agent 使用特定数据库、API、UI 或输入格式。只保留与当前企业相关的指标，不要为了填满结构而创造数值。

## 与业务回答的关系

下面的字段与枚举用于结构化交接和评估，不是默认给员工看的模板。普通对话用自然中文表达同样的含义，不附英文状态码；只有明确要求结构化结果时才按契约输出。

评估 `expected_states` 和 `expected_feasibility` 时，依据回答表达的业务含义及当前对话判断，不要求字面出现枚举值或固定标题。例如“还差本月重点才能定目标”可以表达信息不足；确认时未重复可行性标签，不等于改变上一版结论。模拟输入使用 JSON 不代表模拟用户要求 JSON 输出。

可读性验收看普通员工能否理解当前重点、目标数字、配合事项、预算状态及下一步；追问、修改和确认轮只检查该阶段需要的信息。不得以精简为由放过数据虚构、指标混淆、缺少重要条件或越权，也不要用固定字数代替判断。

请求保存文件时，必须分别评审聊天交付和实际文件，不能用其中一个代替另一个：

- 聊天交付应简短说明文件位置、核心目标、一个重要条件或风险及下一步，不粘贴完整目标表和全部分析；
- 文件应详细到可以脱离聊天独立阅读、转发和确认，覆盖相关历史比较、目标与计算、指标定义、成立条件、人员和内容资源、预算及批准状态、数据来源、检查安排和待确认事项；
- 两者都使用普通员工能理解的业务语言，目标数字、预算状态、可行性和下一步必须一致。

评审时读取实际产物；没有英文状态码不等于可读性合格，篇幅较长也不等于信息详细。检查文件是否机械复制内部字段、反复解释同一事项、仍需翻译抽象术语、把未经验证的下限称为保底，或为了简短而省掉支撑负责人判断的关键依据。

## 月目标与周目标组合

组合模式包含一个月目标、一个不超过七天的简单周目标，以及两者的关系说明。周日期位于月周期内，最多三个指标；分别保留本月累计进度与单周统计范围，不把月度累计值当作周基线。两部分作为同一草案版本展示，用户明确确认整版后才共同生效。只确认月目标时，周目标仍不可交接执行；修改任一部分后重新确认。后续研究和周计划使用周目标，月目标作为背景保留。

宿主工具的具体字段以 Tool Schema 和 [handoff.md](handoff.md) 为准，不要求其他宿主照搬存储格式。原有单周期结构仍适用。

## 结构化目标

```yaml
operation_goal:
  status: proposed

  period:
    start:
    end:

  business_requirement:
    summary:

  business_direction:
  target_users: []
  platform: xiaohongshu

  targets:
    exposure:
      minimum:
      target:
      stretch:
    reads_or_plays:
      minimum:
      target:
      stretch:
    interactions:
      likes:
      saves:
      comments:
      shares:
    private_message_inquiries:
      minimum:
      target:
      stretch:
    inquiries:
      minimum:
      target:
      stretch:
    qualified_leads:
      minimum:
      target:
      stretch:
    appointments:
      minimum:
      target:
      stretch:
    store_visits:
      minimum:
      target:
      stretch:
    conversions:
      minimum:
      target:
      stretch:
    other: []

  resources:
    people: []
    content_capacity:
    sales_or_service_capacity:
    other: []

  paid_media:
    recommended:
    budget_ceiling:
    authorization_status: not_verified

  measurement:
    metric_definitions: []
    data_sources: []
    review_frequency:

  evidence:
    historical_periods: []
    baseline_summary:
    calculations: []

  feasibility:
    verdict:
    reasons: []
    conditions: []

  assumptions: []
  missing_information: []
  conflicts: []
  risks: []

  confidence:
    level:
    reason:

  confirmation:
    status: pending
    note: conversation confirmation only
```

删除不适用的指标，不要把空字段误解为零。输出测试数据时必须明确标记为 `mock`、`fixture` 或 `simulated`。

当前 `platform` 固定为 `xiaohongshu`。不要添加其他运营平台，也不要添加 `profile_visits`、主页访问量或主页访问率字段。微信承接只可体现在私信之后的业务转化、数据来源或资源说明中，不得列为运营平台。

## 工作状态

- `needs_information`：缺少会实质改变目标的关键信息，暂时无法形成有效草案；
- `draft`：已有初步方向，但仍存在重要假设、缺口或冲突；
- `awaiting_confirmation`：信息和可行性说明基本完备，正在等待用户确认；
- `confirmed_in_conversation`：用户在当前对话中明确确认。

结构化目标在确认前保持 `operation_goal.status: proposed`。确认后可改为 `confirmed_in_conversation`，但必须保留其仅代表当前对话确认的说明。

## 可行性状态

- `supported`：现有数据、资源和约束基本支持目标；
- `conditional`：只有满足明确条件后才支持目标；
- `unsupported`：目标与基线、资源或预算明显不匹配，当前没有足够实现路径；
- `insufficient_data`：缺少判断合理性所需的数据。

## 指标要求

每个实际采用的指标应尽量说明：

- 业务定义和统计口径；
- 数据来源；
- 基线周期与基线值；
- 目标区间或最低值、目标值和突破值；
- 复核频率；
- 负责人或数据归属（如果已知）。

不得虚构缺失值。没有可靠基线时，明确写成试运行目标并安排复核节点。

## 确认与预算语义

`confirmed_in_conversation` 不代表确认人身份已验证、目标已持久化、预算已审批或执行已授权。

普通对话可将这四个边界合并为简短说明，不要求四条编号声明。若独立记录已证明其中某项完成，应如实记录，不能因目标刚被确认就擅自升级其他状态；仍须区分对话确认与外部核验、登记、审批和执行。

预算字段必须区分：

- `recommended_budget`：分析建议；
- `budget_ceiling`：不可超过的边界；
- `authorized_budget`：已由有权人员批准的金额；
- `actual_spend`：外部执行产生的实际金额。

本 Skill 只可以生成前两类建议或记录用户明确提供的信息，不执行付费动作。
