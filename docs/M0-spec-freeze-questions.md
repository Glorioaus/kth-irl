# M0 语义待决项清单（spec freeze questions）

日期：2026-09-06　|　性质：M0 未能机械定位或需要 Owner/KTH 专家裁定的业务语义空档。
处置规则：每项必须在对应里程碑以“机械定位 → 台账登记 → 人类裁定（如需）”关闭；
模型不得自行发明答案。

| 编号 | 待决项 | 需要什么 | 关闭里程碑 | 状态 |
|---|---|---|---|---|
| Q1 | **D2 与 D1 的门控关系与投影维度全语义**（D1=NO 时 D2 是否仅含翻转条件分析） | 从 `v1/d2_projections.py`、`v1/decision_authority.py`、CORE_REQUIREMENTS 机械定位并差分验证 | M1 定位 / M4 冻结 | open |
| Q2 | 各维 maturity ladder 的精确级别定义与判据归属 | 从 wheel vertical 代码 + KTH 材料机械提取 | M1 | open |
| Q3 | CRL/BRL 判据总数（现仅有下限：TRL ≥25、TMRL ≥38、IPRL ≥32、FRL ≥36） | baseline.v2 机械提取结果 | M1 | open |
| Q4 | `partial` 在 maturity gate 中“视同未满足”是否与 wheel 行为一致 | 差分测试对照 wheel | M1/M2 | open |
| Q5 | wheel 与源树均存在的 `v1/model_gateway.py` 部署差异对测试环境的可忽略边界 | 按 Owner 定性执行；M1 导入时标注 | M1 | 已有定性（部署差异，非业务） |
| Q6 | 政策七要素中需 Owner 决策的空档（见政策草案 v0 各 [待 Owner 决策] 标记） | Owner 逐项确认后冻结 org-d1-policy.v1 | M0 出 / M3 前冻结 | open |
| Q7 | 封存 Case “研究任务 12/12、专利任务 9/9”与新系统研究规划的任务映射 | M2 分歧台账建立时机械对照 | M2 | open |
| Q8 | 旧 callback 协议（crl_brl 17 / trl_tmrl 17 / iprl 8 / frl 10）承载的判据覆盖面在新 mission 模型下的等价覆盖证明 | coverage matrix 覆盖率核对 | M2 | open |
| Q9 | 2 个显式 abandonment 的失败传输 action（e155ecd…、17edc3…）是否对应旧 Case 报告中已声明的证据缺口 | M2 对照旧 disposition | M2 | open |
| Q10 | 微玖 183 份 qualification 修订件与 80 个 capture 的多对多关系（重资格化时的去重规则） | M2 开工段全量验证时机械归纳 | M2 | open |
| Q11 | **M0.5 链 A 完整 fixture 的确定性来源**：attached source 自带 tests/（81 文件，含 PVS1–6 六维、cross_dimension_decision、d2_projections、investment_report、ceo_decision_brief、local_whole_chain 149 测试）；整链测试以失败路径为主，完成路径以 `test_provider_backed_fixture_completes_product_scopes_and_one_shared_frl`（test_v1_local_whole_chain.py:3253）等 provider-backed 形式存在；组合装配点 = `tests/case_preflight_fixtures.py::build_case_preflight_fixture` + `tests/fixtures/`（terminal-value-workflow 4 件 + kth-hybrid-controller 1 件）。M0.5 开工首日须机械确定完整链 fixture 的确切组成并验证可确定性重放 | M0.5 入口任务：机械定位 + 试运行 | M0.5 | open |

## 登记规则

- 新发现的待决项追加为 Q11+，不得删除已有项；
- 关闭方式：`机械定位（指针）` / `Owner 决策` / `KTH 专家裁定` / `future methodology review`；
- 任何 Q 项关闭前，相关实现不得假设其答案。
