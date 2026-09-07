# kth-irl-hybrid —— KTH IRL Evaluator 整体重建实施仓

本仓库承载 KTH IRL Evaluator 的整体重建（runtime-rebuild）：运行层、内部数据合同、编排、
恢复、决策准入与报告流水线按新合同重建；旧七万行系统不是改造宿主。当前授权：
**《KTH整体重建执行计划v3》，Owner 2026-09-08 批准，仅 R0＋R1**（T01–T06＋I/O 历史交接、
audit/trace、证据 census、R1 交接工件）。工程决定见 `docs/decisions/runtime-v3.md`。

## 当前状态

- 里程碑：**M0 已完成**（权威基线冻结、微玖资产清单冻结、逐模块处置矩阵、政策草案），
  作为历史底稿保留；v3 已取代其"仅至 M0"写入限制与 M0.5 强制门。
- 当前阶段：**R0＋R1 进行中**（分支 `codex/runtime-rebuild`）：可靠原件仓与事务台账、
  全量分层证据盘点、样本资格判断与真实判据消费、trace 与恢复。首批完成即停止送审，
  不自动 R2。
- 真实 Case 工作区：`D:\t\kth-rebuild-cases\<case_id>`（仓库外；原文/盘点/日志不入 Git）。
- 无远端、不 push；发布以单一受审快照回导 sunny-skills（仅凭 Owner 指令）。

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
docs\         决定、规格、检查点；M0 冻结文、分歧台账、政策草案（历史保留）
plugin\       新插件本体（R1 起：src\kth_hybrid、tests；按工作包增量创建）
```

规划中的后续目录（R2+ 按授权创建）：`reference\`（原版源码/测试只读参照）、
`sources\kth\`（KTH 三件方法材料）、`commands\`、`skills\`、`.codex-plugin\`。

## 纪律（详见 AGENTS.md）

- 每里程碑文件 allowlist 制；本仓无条目来源的文件不得存在。
- 判据只能机械提取（代码指针 + 测试指针 [+ 材料指针]），无对应即停。
- 业务结论三分：YES / NO(formal) / NO(insufficient_evidence)；execution_failed 永不转 NO。
- 每 Provider mission 恰好一次（write-ahead 状态机）。
- 凭据：不主动读取、不显示具体值；凭证治理为非阻断 backlog（Owner 2026-09-06 决定）。
- commit、注释与文档使用简体中文。
