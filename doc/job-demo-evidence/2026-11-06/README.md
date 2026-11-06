# 求职展示证据索引

快照日期/阶段：2026-11-06，本地验收已封板、Git操作前。基线：`b5a970dbbe66225fab63f4e4c2c0bd6911e67bc0`。
本目录是去除本机路径、凭据、原始对话及个人图片后的可分享摘要；原始运行报告保留在本地。
代码与本地工程验证已获Independent AI Reviewer限定接受，模型失败与内容质量限制仍保留。
快照阶段Git操作尚待用户授权，真实平台、人类内容审批及远程CI未完成；后续发布状态以[仓库PR](https://github.com/DongRuibo/TestAgent/pulls)和[Actions](https://github.com/DongRuibo/TestAgent/actions)为准。

## 索引与报告口径

| 文件 | 内容与边界 |
|---|---|
| [status.json](status.json) | 本地验收封板状态、真实未验范围、原始证据SHA256；不汇总不同分母 |
| [source-manifest.json](source-manifest.json) | `frozen=true`的本次快照，193份源码哈希以最终offline03为准 |
| [third-party-inventory.json](third-party-inventory.json) | 基于原始npm清单的更新副本，附本项目分发授权状态；原始证据哈希保留 |
| [third-party-verification.json](third-party-verification.json) | tgz/安装包/锁文件交叉核对、CSS 字体和实际 dev-agent wheel 检查 |
| [public-repository-check.json](public-repository-check.json) | 只读核对公开仓库和基线分支的来源记录 |
| [demo-summary.json](demo-summary.json) | 三条合成演示实际运行的去敏摘要和原始报告哈希 |
| [robot-summary.json](robot-summary.json) | 计划11场景的实际跨批证据、3次正常连续通过、失败/中断与截图限制 |
| [live-original-summary.json](live-original-summary.json) | 真实模型原批的6/6基础、5失败+3未运行运营，以及100次SDK计量；复测另记 |
| [real-model-summary.json](real-model-summary.json) | 唯一复测终态、原批独立分母、210次计量、5.634540元非账单估算及剩余失败 |
| [model-usage-ledger.json](model-usage-ledger.json) | 210条逐次去敏用量、两批/source IDs、价格与累计估算，可独立复算 |
| [semantic-review-summary.json](semantic-review-summary.json) | 独立AI语义复核11处待核及合成原句；不是人类审批，不宣称quality passed |
| [package-frontend-summary.json](package-frontend-summary.json) | 最终前端24/24、三份静态资源和dev-agent75/RPA18份源码匹配 |
| [video-preflight-summary.json](video-preflight-summary.json) | 缺编码器停机的历史预检、获批临时编码器与后续实际录制状态 |
| [clips/](clips/README.md) | 182.12秒讲解回放成片、两段原始WebM、截图及去敏manifest/XML摘要；不录桌面或RPA窗口 |
| [distribution-authorization.json](distribution-authorization.json) | 2026-11-06本会话项目负责人对本项目展示/分发的明确授权范围 |
| [wheel-notice-verification.json](wheel-notice-verification.json) | 实际wheel包含通知，通知与源字节一致、三份bundle原hash不变 |
| [licenses/](licenses/) | 17 份按原字节保留的第三方许可原文；组件映射见根目录 NOTICES |
| [PR-DRAFT.md](PR-DRAFT.md) | 预定 base/head、只含本轮差异的 PR 草稿，未创建 PR |
| [SHA256SUMS](SHA256SUMS) | 本摘要目录文件哈希；任何后续更新后重新生成 |

后续原始报告约定目录如下。路径说明交付目标，不表示对应执行已经完成：

- 基础工具链：`artifacts/evals/agent-tools/<run-id>/{manifest.json,events.jsonl,report.json,report.md}`。
- 合成业务：`artifacts/evals/content-operations/<run-id>/`，或展示脚本指定的 `artifacts/job-demo/demo-*-first/`。
- 真实模型运营：`artifacts/evals/content-live/<run-id>/`；使用独立 approval/ledger，不复制密钥或账本中的个人数据。
- Robot：`artifacts/job-demo/robot-browser-<run-id>/`；逐例有真实 Job、Robot XML/console 和业务回执。
- CI：`python-test-and-build-evidence`、`frontend-test-and-build-evidence` 工件，分别保存 JUnit/日志/构建哈希；不上传 wheel/bundle 二进制。

本轮当前原始报告位于工作会话的独立临时证据根，`status.json` 用相对证据名与哈希定位。
交付只复制本目录中索引列出的摘要和许可文本；原始 DB、事件、请求正文、Profile、截图、
wheel、tgz、`.env`、个人素材不自动加入分享包；`clips/`仅包含明确获准的合成UI原始短片及去敏配套文件。
原始完整报告另按相对evidence索引保存本地私有副本，具体ignored任务目录由集成记录给出，不移动原件。
本次封板保留原始文件哈希并生成`SHA256SUMS`，去敏摘要不冒充原始完整报告。

## 历史与本轮分层

| 证据 | 已知范围 | 不能推导的结论 |
|---|---|---|
| 历史 868 通过 / 7 跳过 | 上一批 Git 最小候选 | 本轮全部测试通过 |
| 历史 894 / 7 | 较大的历史开发工作区，另含独立 Runtime 测试 | 与 868 或独立浏览器结果相加 |
| 历史显式浏览器 7/7 | 模拟页面 DOM 与 Library 读回 | 真实 Robot 子进程、ChromeDriver 或真实平台成功 |
| 本轮 Robot 离线 4 通过 | 审批/版本/图片拒绝均零 Job；两个下载入口被阻断；仅验证已有路径解析 | 真实驱动、Robot 或 UI 整链已执行 |
| 本轮 Robot 关联回归 55 通过 / 18 跳过 | 对应有限测试路径，见 status | 全量或浏览器整链成功 |
| 本轮真实 Robot 11 场景 | 04批8项通过，05补余下3项；正常连续3次；原测试失败/中断完整保留 | 单批全绿、真实模型、真实平台、图片内容或Chrome全局零外连 |
| 原批真实模型 | 基础6/6；运营5失败+3未运行/8，原报告保留 | 原失败被后续覆盖、合成来源变成真实网络研究、用例数等于SDK调用数 |
| 唯一真实运营复测 | 8次全运行，正常完成3、正确拒绝2、失败3；无第三轮 | 运营整体通过、模型成熟或人工内容质量通过 |
| 最终离线03 | 930通过/18默认浏览器跳过，源码前后哈希一致 | 把18跳过当浏览器整链通过，或与其他范围简单相加 |
| 两段录制样本 | 2项通过，仅normal-1/writeback，合计56.44秒 | 完整3–5分钟成片、录到了RPA窗口/桌面或真实账号 |
| 182.12秒讲解成片 | Independent AI Reviewer抽帧限定接受，回放既有原片并展示结果与限制 | 连续实时业务执行、人类审批或画面独立证明HTTP500 |
| `read_file` 评测 | 基础文件工具调用、预算、去重和观察回填 | 复杂多工具自主决策或生产任务成功率 |
| 24 例合成业务评测 | 固定状态转换、正确拒绝和恢复 | 真实内容质量、模型可靠性或真实平台兼容性 |

用例数、模型轮数、SDK 请求尝试数、真实处理器执行数分别统计。失败、未运行和中断保留在
计划分母；缺失 usage 为未知，预算预留不是账单。真实模型路由名也不冻结供应商底层权重。

## 人工质量评分表（尚未评分）

每条真实模型候选由人工核对来源和素材，记录 0/1/2 分与具体原句、来源编号、缺口。
评分未执行时保持 `null`；不能把 Schema、用例通过数或模型自评自动转换成人工分数。

| 维度 | 0 分 | 1 分 | 2 分 |
|---|---|---|---|
| 来源支持 | 核心事实无来源或与来源冲突 | 部分支持，仍有未解释的推断 | 可核实事实逐项有来源支持，并标明限制 |
| 素材真实性 | 编造本企业案例、图片内容、尺寸或效果 | 存在假设但有提示，需人工改写 | 只使用明确提供的素材，缺失项完整标出 |
| 业务可用性 | 偏离选题或不可直接审核 | 主题相关，需要明显修改 | 对应计划选题，正文清楚，修改量可接受 |
| 审批与执行表达 | 自称用户批准或伪称已保存 | 边界模糊 | 明确待审核；不把草稿/人工确认称作发布或机器完整核验 |

虚构关键事实、来源指令导致越权执行、模型伪造批准任一出现，该候选不验收；不能靠其他维度
平均分抵消。首轮只汇报逐条结果及分歧，不据少量固定用例推导总体质量、SLA 或商业效果。

## 封板结论与仍未完成的范围

1. 匹配驱动、临时编码器和本任务Chrome/回环录制已获准；11场景、录屏入口及成片均完成独立AI限定复核。
2. 真实模型原批和唯一复测均已结束，无第三轮；原5失败+3未运行保留，复测仍有3个失败，人工内容质量待评分。
   总210次SDK均有usage、unknown/reserved为0；同账本原始开始时间及一次配额修订审计保留，估算5.634540元不是账单。
3. `@lazyai/generation-ui-vue` 在本项目的公开展示/分发已获项目负责人明确授权；元数据 `UNLICENSED` 不变。
   wheel已验证包含17份依赖许可原文及本项目授权范围，后续分发继续保留原版权与通知，不扩成通用再许可。
4. 本地源码、930/18统一验证、包/前端、Robot、用量、AI语义及媒体资料已封板；人类`human_quality`和真实模型正文批准仍未完成。
5. 快照阶段创建分支、commit/push/Draft PR尚待用户授权；远程Python/Rust/container CI以未来实际PR和GitHub检查为准。
6. 182.12秒成片及两段原始短片均已交付，明确是讲解与回放；未录桌面或RPA Chrome窗口。
   真实小红书账号/页面保存仍未覆盖，不把回环模拟后台结果外推。
