# 运营目标信息要求

仅在需要系统检查上下文、判断缺口或处理信息冲突时读取本文件。

这些内容是 Agent 的概念检查模型，不是要求用户填写的表单，也不是固定 API Schema。宿主环境可以提供自然语言、文件、Memory、数据库记录或工具结果；将其理解为下列概念即可。

## 内部信息模型

```yaml
company_context:
  business:
    primary_business:
    priority_products_services:
    region:

  customers:
    target_customers:
    customer_needs:

  operation:
    platform: xiaohongshu
    account_status:
    follower_count:
    historical_content_performance:
    historical_business_conversion:
    current_problems:

  resources:
    available_people:
    content_capacity:
    sales_or_service_capacity:

  budget:
    paid_media_allowed:
    budget_ceiling:
    authorization_status:

business_requirement:
  goal_mode:
  period:
  primary_objective:
  priority_business:
  requested_results:
  known_constraints:

weekly_goal_context:
  parent_goal:
  progress_as_of:
  achieved_results:
  remaining_time:
  recent_weekly_baseline:
  week_specific_constraints:
```

## 信息优先级

根据当前决策动态分类，不要把整个模型机械地变成问卷。

### A：阻塞信息

缺失后无法形成有意义的目标草案，通常包括：

- 目标周期或可接受的周期范围；
- 本周期最重要的经营诉求；
- 企业当前主要业务或重点产品与服务。

如果当前对话已经清楚表达其中一项，不要重复追问。

制定简单周目标时，A 级信息还包括本周的具体起止日期，以及本周要承接的较长周期目标或独立经营重点。当前日期和上下文能明确“本周”“下周”范围时直接换成具体日期，不必为形式完整而追问。

### B：决策相关信息

缺失会明显改变目标数字、渠道或可行性。优先从现有上下文查找；仍缺失时追问或降低置信度：

- 目标客户；
- 当前小红书账号及账号规模；
- 小红书历史曝光、阅读或播放、互动和私信咨询基线；
- 小红书线索进入销售承接后的有效线索、预约、到店等基线；
- 可用人员和内容产能；
- 销售或服务承接能力；
- 投流权限和预算边界。

从月度或其他较长周期目标拆出周目标时，还要优先取得截至明确日期的实际进度、剩余目标、剩余时间和本周特殊资源变化。缺少某项会显著改变周目标数字时再追问；不要在不知道当前进度时直接把整月目标除以四。

没有较长周期目标时，最近可比周的结果属于 B 级信息。无法取得时可以形成试运行周目标，但要明确本周同时用于建立基线。

### C：质量增强信息

可提高判断质量，但通常不阻塞当前草案：

- 小红书历史内容表现；
- 小红书竞争与平台环境；
- 季节性因素；
- 历史投流 ROI；
- 成交和复购数据。

使用会随时间变化的外部信息时，说明数据日期和来源。无法取得时，不要自行补造。

当前平台范围固定为小红书。企业档案中的抖音、视频号、公众号或私域资料不应成为本 Skill 的运营目标；微信只可作为小红书线索的后续承接或记录环节。

不要把“小红书主页访问”列为信息要求，也不要因缺少该指标而追问、阻塞或降低目标完整性。

## 来源与确认状态

重要信息尽量保留下列属性：

```yaml
value:
source:
confirmation_status:
effective_period:
notes:
```

`confirmation_status` 可使用：

- `confirmed`：用户在当前对话中明确确认；
- `reported`：由用户或业务方陈述，但未独立验证；
- `document_derived`：来自文件、知识库或系统资料；
- `inferred`：Agent 基于现有事实推导；
- `unknown`：未知；
- `conflicted`：不同来源不一致。

不要把 `reported`、`document_derived` 或 `inferred` 自动升级为 `confirmed`。

## 最少追问规则

1. 只问会改变目标方向、数值或可行性的内容；
2. 把相关缺口合并成自然的经营问题；
3. 每轮通常不超过三个问题；
4. 能给出有意义草案时，允许带明确假设和较低置信度继续；
5. A 级信息缺失或关键数据冲突时，停在 `needs_information`；
6. 不要求用户提供内部字段名、固定 JSON 或完整资料表。

## 冲突处理

发现目标周期、重点业务、预算、历史基线或资源信息冲突时：

1. 列出各版本及来源；
2. 说明冲突将影响什么决策；
3. 请求用户确认采用哪个版本；
4. 确认前不要把相关数值写成确定事实，也不要进入 `awaiting_confirmation`。
