# 业务合同规格 business-contract-v1（R1 实现范围）

日期：2026-09-08。依据：《KTH整体重建执行计划v3》§3。
性质：内部数据合同规格（R1 子集），不是 KTH 方法批准。合同名：`kth-hybrid.formal-admission.v1`
的 R1 前置部分——本文件只冻结 R1 必需的对象与状态；Decision/ReportBundle 等正式对象在
R3 前不入实现，本文件仅登记其边界以防 R1 误写。

## 1. 数据流

```text
附件/原文 → 原件(Source) → 可定位文本 → 主张候选(Claim) → 资格判断(Qualification/Gap)
                                                    ↓
主体/评估单元/截止/方法版本 → 冻结证据视图(EvidenceView) → 判据消费(CriterionResult)
```

R1 只需打通到 CriterionResult（单判据真实入口），不实现六维全量、决策与报告。

## 2. R1 必需记录（SQLite 表 + 仓库外原件）

| 记录 | 必需字段与约束 |
|---|---|
| Case | case_id、subject、proposal、action_scope、evidence_cutoff、report_date、assessment_units、method_profile、policy_version；截止与报告日期不同 |
| Source | source_id、content_sha256、byte_length、media_type、locator、retrieved_at、published_at/event_time 及其证明、source_family、原始采集状态；日期未知允许 null 并记原因 |
| Claim | claim_id、source_id、定位区间或页面、原文/抽取文本 hash、解释文本、subject_scope、interpretation_attempt；候选不是事实 |
| Qualification | claim_id、source/identity/time/independence 四类判断及依据、allowed_uses、cannot_prove、review_attempt、status；字段不能只填"valid"而无依据 |
| Gap | gap_id、缺口类型、影响的 criterion/风险、是否管线故障、已完成调查及无法确认项；不虚构 Source |
| CriterionResult | criterion_id、native_disposition、product_status、evidence_refs、gap_refs、rationale、scope、rule_version；不将产品不足状态伪装为原生已支持 |

`native_disposition` 在无法调用原版或其无对应状态时为 null 并保留原因。产品的
`insufficient`、`method_unsupported`、`execution_failed` 三个产品状态互不互换，后两者
不得映射成业务 NO。

## 3. 状态与恢复

- Case 阶段：`intake → evidence → dimensions → decision → report → delivered`；每阶段执行
  状态独立 `pending/running/succeeded/blocked/failed`。阶段名不是凭证。
- 外部任务状态：`planned → claimed → dispatch_recorded → succeeded/failed/outcome_unknown`；
  本地已记录 dispatch 但未取得持久结果时保守视为未知；无 Provider 幂等/查单能力不自动重发。
- SQLite 保存任务唯一性、认领和阶段提交；原始请求/响应及原件落文件后才提交引用。
- 文件写入失败、DB 提交失败、进程崩溃、重复进程都必须有测试；孤立文件保留为未提交工件。
- 每次输入变化建立新的 run 与视图，不修改旧结果。

## 4. R1 最小审计链（v3 §3.3.1）

```text
CriterionResult → JudgmentCandidate/规则版本 → Qualification → Claim
  → Source 及精确定位 → 封存原字节
```

每条边存实际生成/使用的身份，不得只生成"链完整=true"收据。缺口路径到达真实 Gap 及调查
记录。读 trace 时重新检查相关对象和摘录 hash；修改原文或断开资格引用必须触发可见失败。
失败记录至少含 stage、task/attempt、输入身份、错误类别、已发生/未知的外部动作和已有工件
位置。重试新建 attempt，旧 attempt 保留。

本地单机追加约束和 hash 只支持篡改检测与重放检查，不宣传为外部公证。

## 5. 明确不在 R1 的对象（防误写）

Decision、ReportBundle、RoleAttempt 的真实模型部分、policy/admission——R2/R3 实现。
R1 的 runner/cli 只输出"核验记录"，标题明确非正式评估报告。
