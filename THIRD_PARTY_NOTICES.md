# 第三方组件与分发边界

本文件记录组件归属、原文通知及项目负责人对本项目的授权范围，不更换包许可证。核查日期：2026-11-06。
源码定位、工具/业务实现属于本项目维护范围；模型、SDK、云服务和浏览器基础设施为所依赖能力。
Generation UI是本项目复用渲染组件，本轮实现桥接与业务卡片，未在本轮实现组件引擎；不据包名推断用户既往是否参与组件研发。

## 本项目的授权范围

`@lazyai/generation-ui-vue@0.5.2` 的 tgz `package.json` 明确声明 `UNLICENSED`，归档内没有 LICENSE/NOTICE。
原[组件指南第27节](生成式ui/组件交付与原生HTML-Agent接入指南.md#27-授权与分发)要求对外分发前先确认知识产权和许可证授权。
项目负责人于2026-11-06在本会话明确声明：“我拥有完整权利，允许公开展示和分发”。
本项目公开展示和分发据此获得明确授权，见[授权范围记录](doc/job-demo-evidence/2026-11-06/distribution-authorization.json)。
原 `UNLICENSED` 元数据不变；本记录不将组件改为MIT等通用许可证，不扩展为其他项目的通用再许可，
也不声称另行查验了外部权利人合同或书面授权证明。其他依赖仍遵守各自原许可。

本轮于2026-11-06通过GitHub只读连接器核对仓库为public，公开分支
[`codex/personal-agent-monorepo`](https://github.com/DongRuibo/TestAgent/tree/codex/personal-agent-monorepo)
指向基线 `b5a970dbbe66225fab63f4e4c2c0bd6911e67bc0`。该基线已跟踪 tgz 及三份静态 bundle；
本地历史显示 tgz 始于提交 `63383b3`。这说明需要核查现存公开范围，不能通过本轮删除文件或改写历史声称问题消失。

## 运行时依赖与打包资产

| 范围 | 已核查事实 | 未覆盖事项 |
|---|---|---|
| 前端npm依赖 | 锁文件34个非dev条目与本机已安装版本一致；其许可元数据见下表 | 非dev条目不等于每个包的全部代码都进入bundle；开发依赖没有做完整传递许可审计 |
| 静态JS/CSS | 三份跟踪资源的SHA256与原清单一致；Vue/PrimeVue/Chart.js等由Vite打包 | 未从无sourcemap的bundle精确归因每个代码片段 |
| PrimeIcons字体 | CSS内嵌EOT/WOFF2/WOFF/TTF的5个URL逐字节匹配原包（EOT重复）；SVG元素/属性匹配，元数据引号空白已变换 | SVG不是原始字节相同；未主张新字体许可 |
| Python运行时依赖 | pyproject/uv.lock声明FastAPI、OpenAI、RPA等外部分发包；dev-agent wheel用Requires-Dist引用这些依赖 | 本文件不是整个Python传递依赖树的法律审查；没有把依赖代码全部打进本项目wheel |
| dev-agent wheel | 临时源码副本离线重建后含三份原hash的bundle及`web_agent/THIRD_PARTY_NOTICES.txt`，notice与源字节一致；未发布 | 该通知明确本项目授权范围，不设置项目整体许可证或其他项目再许可 |

完整包名、版本、资源/字体哈希和wheel检查见[核查JSON](doc/job-demo-evidence/2026-11-06/third-party-verification.json)。
原始[锁文件清单](doc/job-demo-evidence/2026-11-06/third-party-inventory.json)是34个runtime条目的来源；
[前端入口](apps/dev-agent/frontend/src/generation-ui-bridge.js)、[Vite配置](apps/dev-agent/frontend/vite.config.js)和
[Python package-data配置](apps/dev-agent/pyproject.toml)说明它们如何进入静态页面和wheel。

## 保留的许可原文

以下17份文本从已安装包按原字节复制，版权声明未改写；同一原文合并存放，包与版本保持逐项映射。
`UNLICENSED` 条目保留包元数据，并附本会话项目负责人对本项目展示/分发的授权范围；不会用其他依赖的MIT许可覆盖它。

| 组件及版本 | 原包许可元数据 | 保留原文 |
|---|---|---|
| `@babel/helper-string-parser@7.29.7`；`@babel/helper-validator-identifier@7.29.7`；`@babel/types@7.29.8` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-117da2af0d4c.txt) |
| `@babel/parser@7.29.8` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-2e97627cb278.txt) |
| `@jridgewell/sourcemap-codec@1.5.5` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-769d154fbde3.txt) |
| `@kurkle/color@0.3.4` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-89ba00327314.txt) |
| `@lazyai/generation-ui-vue@0.5.2` | UNLICENSED | [本项目授权范围](doc/job-demo-evidence/2026-11-06/distribution-authorization.json) |
| `@primeuix/styled@0.7.4`；`@primeuix/styles@2.0.3`；`@primeuix/themes@1.2.5`；`@primeuix/utils@0.6.4` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-c6e70ce414f6.txt) |
| `@primevue/core@4.5.5`；`@primevue/icons@4.5.5`；`primevue@4.5.5` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-39a2ce8d759c.txt) |
| `@vue/compiler-core@3.5.41`；`@vue/compiler-dom@3.5.41`；`@vue/compiler-sfc@3.5.41`；`@vue/compiler-ssr@3.5.41`；`@vue/reactivity@3.5.41`；`@vue/runtime-core@3.5.41`；`@vue/runtime-dom@3.5.41`；`@vue/server-renderer@3.5.41`；`@vue/shared@3.5.41`；`vue@3.5.41` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-1bb85cc9b13b.txt) |
| `chart.js@4.5.1` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-41a84aa2caba.txt) |
| `csstype@3.2.3` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-11d55bd4541c.txt) |
| `entities@7.0.1` | BSD-2-Clause | [原文](doc/job-demo-evidence/2026-11-06/licenses/bsd-2-clause-cb992345949c.txt) |
| `estree-walker@2.0.2` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-8a6dcbabe717.txt) |
| `magic-string@0.30.21` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-1cbe51b90766.txt) |
| `nanoid@3.3.18` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-da4db1480d9b.txt) |
| `picocolors@1.1.1` | ISC | [原文](doc/job-demo-evidence/2026-11-06/licenses/isc-6582629e2979.txt) |
| `postcss@8.5.26` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-5be1f3465bba.txt) |
| `primeicons@7.0.0` | MIT | [原文](doc/job-demo-evidence/2026-11-06/licenses/mit-140e5886a54d.txt) |
| `source-map-js@1.2.1` | BSD-3-Clause | [原文](doc/job-demo-evidence/2026-11-06/licenses/bsd-3-clause-6cb0631f71c7.txt) |

普通CI只上传JUnit、测试/构建日志及哈希摘要；未在本轮新增wheel或bundle二进制上传。
17份原文与组件/版本映射也汇总于[随wheel打包的通知](apps/dev-agent/src/web_agent/THIRD_PARTY_NOTICES.txt)。
后续分发继续保留通知和原版权声明；不得把本会话的项目范围许可改写为组件通用开源许可。
本轮未变更任何组件许可证，没有删除tgz、替换组件或改写Git历史。
