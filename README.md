# kth-irl-hybrid —— KTH IRL Evaluator 混合式 Clean-Slate 重构

本仓库是 KTH IRL Evaluator 的全新独立实现线（Hybrid Clean-Slate）：可信业务内核从原版
抽取复用，产品外壳彻底重写。依据《KTH IRL Hybrid Clean-Slate 实施计划 v1.1》（2026-09-06
获批，仅批准至 M0）与 Owner 同日最终覆盖指令建立。

## 当前状态

- 里程碑：**M0 已完成**（权威基线冻结、微玖资产清单冻结、逐模块处置矩阵、政策草案）。
- 里程碑顺序（Owner 2026-09-06 硬门补充）：M0 审核 → **M0.5 Walking Skeleton 可行性
  证明（强制，3–5 工作日量级，失败即停止整个当前实现方案）** → GPT-5.6-sol fresh-context
  审核 → Owner 决定是否批准 M1。原 M1–M7 未自动获批。
- 本仓库当前不含任何业务代码；M0.5/M1 起经审计导入原版参照资产并实现。
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
provenance\   外部资产来源清单（M1 起的审计导入台账）与 SHA256SUMS
docs\         M0 冻结文、分歧台账、政策草案；后续里程碑文档
```

规划中的后续目录（M1 起，按里程碑 allowlist 创建）：

```
reference\    经审计导入的原版源码/测试（只读参照，非发布件）
registry\     七 Registry v2
sources\kth\  KTH 三件方法材料
plugin\       新插件本体（src\kth_hybrid、commands、skills、tests、tools、deploy）
```

## 纪律（详见 AGENTS.md）

- 每里程碑文件 allowlist 制；本仓无条目来源的文件不得存在。
- 判据只能机械提取（代码指针 + 测试指针 [+ 材料指针]），无对应即停。
- 业务结论三分：YES / NO(formal) / NO(insufficient_evidence)；execution_failed 永不转 NO。
- 每 Provider mission 恰好一次（write-ahead 状态机）。
- 凭据：不主动读取、不显示具体值；凭证治理为非阻断 backlog（Owner 2026-09-06 决定）。
- commit、注释与文档使用简体中文。
