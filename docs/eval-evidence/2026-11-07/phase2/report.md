# TestAgent 离线 Agent Evaluation 固定基线

场景契约通过：144/150。模式为固定 SDK 脚本，不是模型完成率。

| 指标 | 分子 | 分母 | 值 | 未观测 |
|---|---:|---:|---:|---:|
| task_success_rate | 27 | 27 | 1.0 | 0 |
| state_consistency_rate | 96 | 99 | 0.9696969696969697 | 0 |
| recovery_success_rate | 42 | 45 | 0.9333333333333333 | 0 |
| idempotency_success_rate | 24 | 24 | 1.0 | 0 |
| tool_call_accuracy | 15 | 15 | 1.0 | 0 |
| duplicate_side_effect_rate | 0 | 30 | 0.0 | 0 |
| unauthorized_action_rate | 0 | 36 | 0.0 | 0 |
| false_completion_rate | 3 | 9 | 0.3333333333333333 | 0 |

离线延迟：{'p50': 21.0593749943655, 'p95': 1041.1355000105686, 'observed': 150, 'planned': 150, 'scope': 'scripted_per_case_includes_fixture_and_fault_wait_not_model_or_web_SLA'}

## 全部场景

| 场景 | 轮次 | 状态 | 通过 | 失败原因 |
|---|---:|---|---|---|
| AG-A01 | 1 | passed | True |  |
| AG-A02 | 1 | passed | True |  |
| AG-A03 | 1 | passed | True |  |
| AG-A04 | 1 | passed | True |  |
| AG-A05 | 1 | passed | True |  |
| AG-A06 | 1 | passed | True |  |
| AG-B01 | 1 | passed | True |  |
| AG-B02 | 1 | passed | True |  |
| AG-B03 | 1 | passed | True |  |
| AG-B04 | 1 | passed | True |  |
| AG-B05 | 1 | passed | True |  |
| AG-B06 | 1 | passed | True |  |
| AG-B07 | 1 | passed | True |  |
| AG-B08 | 1 | passed | True |  |
| CO-N01 | 1 | completed | True |  |
| CO-N02 | 1 | completed | True |  |
| CO-N03 | 1 | completed | True |  |
| CO-N04 | 1 | completed | True |  |
| CO-N05 | 1 | completed | True |  |
| CO-N06 | 1 | completed | True |  |
| CO-I01 | 1 | completed | True |  |
| CO-I02 | 1 | completed | True |  |
| CO-I03 | 1 | completed | True |  |
| CO-I04 | 1 | completed | True |  |
| CO-I05 | 1 | completed | True |  |
| CO-I06 | 1 | completed | True |  |
| CO-A01 | 1 | completed | True |  |
| CO-A02 | 1 | completed | True |  |
| CO-A03 | 1 | completed | True |  |
| CO-A04 | 1 | completed | True |  |
| CO-R01 | 1 | completed | True |  |
| CO-R02 | 1 | completed | True |  |
| CO-R03 | 1 | completed | True |  |
| CO-R04 | 1 | completed | True |  |
| CO-R05 | 1 | completed | True |  |
| CO-R06 | 1 | completed | True |  |
| CO-P01 | 1 | completed | True |  |
| CO-P02 | 1 | completed | True |  |
| RT-E01 | 1 | completed | True |  |
| RT-E02 | 1 | completed | True |  |
| RT-E03 | 1 | completed | True |  |
| RT-E04 | 1 | completed | True |  |
| RT-E05 | 1 | completed | True |  |
| RT-E06 | 1 | completed | True |  |
| RT-E07 | 1 | completed | True |  |
| RT-E08 | 1 | completed | True |  |
| RT-E09 | 1 | completed | True |  |
| RT-E10 | 1 | completed | False | receipt_matches_database |
| RT-E11 | 1 | completed | True |  |
| RT-E12 | 1 | completed | False | no_false_completion |
| AG-A01 | 2 | passed | True |  |
| AG-A02 | 2 | passed | True |  |
| AG-A03 | 2 | passed | True |  |
| AG-A04 | 2 | passed | True |  |
| AG-A05 | 2 | passed | True |  |
| AG-A06 | 2 | passed | True |  |
| AG-B01 | 2 | passed | True |  |
| AG-B02 | 2 | passed | True |  |
| AG-B03 | 2 | passed | True |  |
| AG-B04 | 2 | passed | True |  |
| AG-B05 | 2 | passed | True |  |
| AG-B06 | 2 | passed | True |  |
| AG-B07 | 2 | passed | True |  |
| AG-B08 | 2 | passed | True |  |
| CO-N01 | 2 | completed | True |  |
| CO-N02 | 2 | completed | True |  |
| CO-N03 | 2 | completed | True |  |
| CO-N04 | 2 | completed | True |  |
| CO-N05 | 2 | completed | True |  |
| CO-N06 | 2 | completed | True |  |
| CO-I01 | 2 | completed | True |  |
| CO-I02 | 2 | completed | True |  |
| CO-I03 | 2 | completed | True |  |
| CO-I04 | 2 | completed | True |  |
| CO-I05 | 2 | completed | True |  |
| CO-I06 | 2 | completed | True |  |
| CO-A01 | 2 | completed | True |  |
| CO-A02 | 2 | completed | True |  |
| CO-A03 | 2 | completed | True |  |
| CO-A04 | 2 | completed | True |  |
| CO-R01 | 2 | completed | True |  |
| CO-R02 | 2 | completed | True |  |
| CO-R03 | 2 | completed | True |  |
| CO-R04 | 2 | completed | True |  |
| CO-R05 | 2 | completed | True |  |
| CO-R06 | 2 | completed | True |  |
| CO-P01 | 2 | completed | True |  |
| CO-P02 | 2 | completed | True |  |
| RT-E01 | 2 | completed | True |  |
| RT-E02 | 2 | completed | True |  |
| RT-E03 | 2 | completed | True |  |
| RT-E04 | 2 | completed | True |  |
| RT-E05 | 2 | completed | True |  |
| RT-E06 | 2 | completed | True |  |
| RT-E07 | 2 | completed | True |  |
| RT-E08 | 2 | completed | True |  |
| RT-E09 | 2 | completed | True |  |
| RT-E10 | 2 | completed | False | receipt_matches_database |
| RT-E11 | 2 | completed | True |  |
| RT-E12 | 2 | completed | False | no_false_completion |
| AG-A01 | 3 | passed | True |  |
| AG-A02 | 3 | passed | True |  |
| AG-A03 | 3 | passed | True |  |
| AG-A04 | 3 | passed | True |  |
| AG-A05 | 3 | passed | True |  |
| AG-A06 | 3 | passed | True |  |
| AG-B01 | 3 | passed | True |  |
| AG-B02 | 3 | passed | True |  |
| AG-B03 | 3 | passed | True |  |
| AG-B04 | 3 | passed | True |  |
| AG-B05 | 3 | passed | True |  |
| AG-B06 | 3 | passed | True |  |
| AG-B07 | 3 | passed | True |  |
| AG-B08 | 3 | passed | True |  |
| CO-N01 | 3 | completed | True |  |
| CO-N02 | 3 | completed | True |  |
| CO-N03 | 3 | completed | True |  |
| CO-N04 | 3 | completed | True |  |
| CO-N05 | 3 | completed | True |  |
| CO-N06 | 3 | completed | True |  |
| CO-I01 | 3 | completed | True |  |
| CO-I02 | 3 | completed | True |  |
| CO-I03 | 3 | completed | True |  |
| CO-I04 | 3 | completed | True |  |
| CO-I05 | 3 | completed | True |  |
| CO-I06 | 3 | completed | True |  |
| CO-A01 | 3 | completed | True |  |
| CO-A02 | 3 | completed | True |  |
| CO-A03 | 3 | completed | True |  |
| CO-A04 | 3 | completed | True |  |
| CO-R01 | 3 | completed | True |  |
| CO-R02 | 3 | completed | True |  |
| CO-R03 | 3 | completed | True |  |
| CO-R04 | 3 | completed | True |  |
| CO-R05 | 3 | completed | True |  |
| CO-R06 | 3 | completed | True |  |
| CO-P01 | 3 | completed | True |  |
| CO-P02 | 3 | completed | True |  |
| RT-E01 | 3 | completed | True |  |
| RT-E02 | 3 | completed | True |  |
| RT-E03 | 3 | completed | True |  |
| RT-E04 | 3 | completed | True |  |
| RT-E05 | 3 | completed | True |  |
| RT-E06 | 3 | completed | True |  |
| RT-E07 | 3 | completed | True |  |
| RT-E08 | 3 | completed | True |  |
| RT-E09 | 3 | completed | True |  |
| RT-E10 | 3 | completed | False | receipt_matches_database |
| RT-E11 | 3 | completed | True |  |
| RT-E12 | 3 | completed | False | no_false_completion |

- 脚本固定模型决策，不估计真实模型成功率、语言内容质量、Token或费用。
- 副作用是合成账本；没有运行 Chrome、Robot、真实账号或生产数据库。
- 新探针保留 SQLite；复用套件只保留生产数据库读回的结构化快照。
- 提交退出实验是受控 os._exit，不代表断电、磁盘故障或多实例可靠性。
- 混合场景耗时包含夹具、子进程启动与注入等待；只适合作为同环境离线基线。
