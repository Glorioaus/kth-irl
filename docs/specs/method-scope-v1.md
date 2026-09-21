# 方法范围规格 method-scope-v1

日期：2026-09-08。依据：《KTH整体重建执行计划v3》§1.2/§1.3、架构裁决 A3 节、
baseline-probe.json（2026-09-07 从批准 wheel 的六个 `get_*_registry()` 直接取得）。
性质：登记既有方法实现范围与已知差距；**不是**方法批准，不扩大也不缩小方法含义。

## 1. 批准 wheel 当前实现的判据范围

| 维度 | 判据数 | 支持级别 | 原生 dispositions |
|---|---|---|---|
| CRL | 13 | 1–4 | met / not_met / partial / not_applicable（**无 insufficient**） |
| BRL | 36 | 1–9 | 含 insufficient |
| TRL | 25 | 1–9 | 含 insufficient |
| TMRL | 38 | 1–9 | 含 insufficient |
| IPRL | 32 | 1–9 | 含 insufficient |
| FRL | 36 | 1–9 | 含 insufficient |
| 合计 | **180** | — | — |

- 180 条是**批准 wheel 当前实现的条目**，不是完整 KTH 判据全集；各 registry 均带
  `internal_shadow=true`、`official_kth_assessment=false` 声明。
- 不得机械定位原版测试覆盖的判据项，明确列为缺口（见 §4）。

## 2. 已知方法范围差距（登记，不自行裁定）

| 差距 | 事实 | R1 行为 |
|---|---|---|
| CRL 仅 1–4 级 | KTH Compiled F 材料第 3–4 页列出 1–9 级；wheel getter 只实现 1–4 | 达到支持上限时标记"范围未覆盖"，不伪报完整 CRL4+，不因此给业务 NO；R2 形成方法范围裁定包 |
| CRL 无原生 insufficient | CRL `_DISPOSITIONS` 四值；其他五维有 | 产品 `insufficient` 状态与原生状态分列；新增状态属显式合同变更，单列 |
| FRL 受限 N/A | 仅在明确不计划外部融资且对应 `na_policy=explicit_no_external_financing_only` 时接受 N/A（FRL4/5/6/7/8/9 的 PITCH 及 6–9 的 STATUS/相关行；`never` 行不允许）；成熟度累计检查接受 met 或该种 N/A | 不得用统一算法消掉该例外；非法 N/A 必须被拒绝 |
| TMRL 身份叠加 | identity overlay 强制但不得直接设定 TMRL；`identity_overlay_may_set_readiness=false`；关系非自动惩罚 | 身份不明的共享证据不能证明 team-specific 主张 |
| 低成熟度非积极成就 | CRL1/FRL1 等含"尚无明确假设/资金/认知"的发展状态描述 | 不把低级判据机械改成"需要正面商业证明"；搜索零结果不证明"不存在" |
| 全零证据合同 | TRL 要求至少一条 Evidence 且每条 adjudication 引用非空 | 无素材时不造 Evidence 通过合同；如实形成 Gap |

## 3. 六维一致保留的语义（不可统一）

- 适用性与逐级成熟度按维度处理；六维不可由总分替代。
- 各维 N/A 约束并不一致（KTH Dashboard Instructions 允许跳过确实不相关判据，但各
  vertical 实现各自约束）。
- 维度边界（owns/does_not_own）按各 registry 原文保留；共享证据规则
  `same_evidence_identity_requires_dimension_specific_claim_scope_and_inference`。

## 4. 测试指针缺口

原版 81 个测试文件中可机械定位到判据行为的映射在 T02 索引中登记；无法机械定位的项
列为缺口，不猜测补写。本轮不把 78 项组件测试（56 通过/22 错误）当作判据行为已验证
的全集——22 个错误均为 Windows 目录替换 WinError5，因果范围见 I/O 历史交接。
