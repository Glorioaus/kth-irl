# kth-irl-hybrid —— KTH IRL Evaluator 整体重建实施仓

本仓库是唯一后续工作仓库，承载KTH IRL Evaluator的运行层重建。保留已有成功能力，
按合同分模块替换，不再把旧七万行系统作为实现宿主，不另建第三个实现仓。

## 从这里继续

**先读[唯一CURRENT](docs/CURRENT.md)**，其中集中维护当前状态、有效授权、验收边界和下一动作。
本README不维护第二份进度，也不把旧检查点当成最新状态。

- [完整执行输入](docs/plans/N1/新执行对话完整输入.md)：供新对话使用，是否开工仍取决于Owner明确指令。
- [真实可检查窄闭环阶段计划](docs/plans/N1/真实可检查窄闭环阶段计划.md)：P1–P3连续离线工作包与G1/G2/G3审核门。
- [工作目录与收口说明](docs/工作目录与收口说明.md)：文件归属、历史入口及尚存运行依赖。
- [参考资料入口](docs/reference/README.md)：原始需求、版本、样本输入输出和成功/失败参考；按需展开。
- [工程路线](docs/decisions/runtime-v3.md)与[工作纪律](AGENTS.md)：保留不冲突的业务不变量。

## 权威基线（详见 baseline/kth-hybrid-baseline.v1.json）

| 项 | 钉定值 |
|---|---|
| 唯一执行 oracle | wheel `kth_irl_evaluator-0.1.5-py3-none-any.whl`，sha256 `2c490508…471fe`（M0 复验逐字节一致） |
| 唯一源码解释基线 | 主仓 attached source（81 模块，80 与 wheel 一致，唯一差异 `v1/model_gateway.py` = 已批准部署差异） |
| 漂移失败案例 | 旧 rev744 worktree 源（58/81 一致，23 模块漂移 + 11 个 staged 私有模块），禁止导入 |
| 完全隔离 | `kth-0.15-0828` 未授权快照、rev744 session 状态、family-runs、旧 stage runtime |
| 业务分歧裁定 | Owner 最高；未被 Owner 改变的业务行为以 wheel 为 canonical；KTH 材料仅解释；模型无裁定权 |

## 目录

```
baseline\     权威基线钉定（哈希、复验结果、禁入清单）
provenance\   外部资产来源清单（审计导入台账）与 SHA256SUMS
docs\         唯一CURRENT、现行阶段计划、合同、参考索引与历史检查点
plugin\       新插件本体（R1 起：src\kth_hybrid、tests；按工作包增量创建）
.local\       Git忽略的私有原件、未来运行/Case/报告与本次收口证据，不代表可删
```

旧Case和历史仓继续只读；新运行目录按获准阶段在`.local/runs/`内创建。正常catalog已使用
包内固定数据，不再依赖历史仓wheel；所需Case和参考源码有仓内保护快照。详见收口说明，
不要将工作仓自包含或离线包验证扩大成完整产品/宿主交付验收。

## 纪律（详见 AGENTS.md）

- 每阶段文件allowlist与来源记录制；本次文档/资料按AGENTS现行补充登记，不改历史来源账。
- 判据只能机械提取（代码指针 + 测试指针 [+ 材料指针]），无对应即停。
- 业务结论三分：YES / NO(formal) / NO(insufficient_evidence)；execution_failed 永不转 NO。
- 每 Provider mission 恰好一次（write-ahead 状态机）。
- 凭据：不主动读取、不显示具体值；凭证治理为非阻断 backlog（Owner 2026-09-06 决定）。
- commit、注释与文档使用简体中文。
