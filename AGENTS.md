# AGENTS.md —— 本仓库工作纪律（对所有实现/审核 Agent 生效）

**当前上位文件（2026-09-08 起）：**《KTH整体重建执行计划v3》＋ Owner R2-A 首批授权。
本批只扩展到 T07 的 CRL 13 条、1–4级离线维度切片、180条范围清单及最终census更正；
T08/T09、其他五维、真实模型/Provider、D1/D2、报告与发布仍未授权。工程决定记录见
`docs/decisions/runtime-v3.md`。旧上位文件《Hybrid v1.1 实施计划》降为 M0 历史。

按开工授权，以下旧约束**仅在 R0＋R1 期间被取代**：M0-only 写入限制（plugin\ 目录自 T02
起按工作包写代码/测试）；M0.5 Walking Skeleton 强制门（被 R1 验收口径取代：真实证据存量
表、核据消费、trace、恢复记录）；"48 模块抽取矩阵"；"C/薄入口优先、原版完整 D1/D2 接口
尽量沿用"。旧审计文件只用于磁盘事实和反例，不能选择性恢复已废止路线。

仍然完全有效的纪律（未被取代）：

- **原始资产只读**：主仓 `D:\UGit\sunny-skills`（dev）、旧 staged worktree、rev744 封存
  session、family-runs、旧 stage runtime、`kth-0.15-0828`、旧 receipts/gates。
- **凭证**：不读取、不打印、不显示具体值；凭证治理为非阻断 backlog（§4 原文保留）。
- **业务不变量**（v3 §1.2）：真实原件不改写；来源/身份/时间不合格不得提升成熟度；无合格
  证据不得 met；六维不可总分替代；系统/方法失败不得转业务 NO；未知外部结果不盲重发。
- **禁止**：monkeypatch 原版模块、validator bypass、模型补写 Evidence/成熟度/结论、
  fixture 冒充真实 Case、merge/push/tag/release、配置远端。
- **网络**：首批零网络、零真实 Provider/运行时模型 API、不联网装依赖。
- **写入范围**：仓库内仅 T01–T06 计划文件＋项目环境/忽略规则/R1 检查点（逐项记理由）；
  真实 Case 数据只写 `D:\t\kth-rebuild-cases\<case_id>`（仓库外）；测试只写受控临时目录
  和新 Case 目录。
- **语言与提交**：简体中文沟通/注释/文档/本地提交；不 push。

以下为 M0 时期原文（历史保留，冲突处以上节为准）：

---

上位文件：《KTH IRL Hybrid Clean-Slate 实施计划 v1.1》（2026-09-06 批准，仅至 M0）与
Owner 2026-09-06 最终覆盖指令。本文与其冲突时以上位文件为准。

## 1. 范围与红线

- 本仓库是 KTH IRL Evaluator 的独立新实现线；主仓 `D:\UGit\sunny-skills`（dev 分支）、
  旧 staged worktree、rev744 封存 session、family-runs、旧 stage runtime 一律**只读**。
- 禁止：monkeypatch 原版模块、validator bypass、以模型补写 Evidence/RawCapture/成熟度/
  判据/最终结论、以测试 fixture 冒充真实 Case、merge/push/tag/release、配置远端。
- 禁止导入（provenance/manifest.v1.json -> forbidden_sources 全表）：旧 worktree 漂移源
  （23 漂移 + 11 staged 私有模块）、kth-0.15-0828、rev744 状态/receipts、family-runs。
- 唯一例外：微玖证据资产池（附件/RawCapture/receipt）按 golden_case 条目复用，且仅在
  哈希复验通过后进入仓库外 Case 工作区。

## 2. 权威与裁定

- Owner 明确业务要求最高；未被 Owner 改变的业务行为以批准 wheel
  `2c490508…471fe` 为 canonical。
- KTH 三件材料仅用于解释与发现差异，**不覆盖 wheel 行为**；冲突时模型不得改算法，
  登记 `docs/divergence-ledger.md`，由 Owner/KTH 专家裁定；无专家则维持 wheel 行为并
  登记 future methodology review。
- 判据（criterion）只能机械提取：每条必须携带 code_pointer（80 模块基线内位置）+
  test_pointer（原版测试）+（可机械定位时的）material_pointer；无法机械对应即停止
  并登记，不得猜测补写。

## 3. 工程纪律

- **里程碑 allowlist 制**：每里程碑开工前确定文件 allowlist；allowlist 外文件禁止创建。
- **来源台账制**：任何文件入仓库前必须在 provenance/manifest.v1.json 有条目（源路径、
  源身份、sha256、许可与归属、处置）；无条目文件视为违规，CI 应拦截。
- 业务不变量见 `docs/M0-business-invariants.md`（INV-01…INV-20），全部为测试可执行项。
- 决策三分：`YES | NO(formal) | NO(insufficient_evidence) | DECISION_UNAVAILABLE`；
  模型/角色失败属系统失败，永不转写为业务 NO。
- Provider mission 恰好一次：planned → claimed_before_dispatch → dispatched →
  succeeded/failed/outcome_unknown → sealed；状态先落盘再行动；恢复只读 sealed capture；
  outcome_unknown 禁止自动重试（唯一例外：已验证的 Provider idempotency key 同键恢复）。
- 真实 Case 数据与凭据不入 Git；Case 工作区在仓库外；真实 kth-env 仅存在于本地部署位。

## 4. 凭据（Owner 2026-09-06 最终决定）

- 凭证治理（轮换、Git 历史、扫描整改）为**非阻断 backlog**，不得因此停止实现、测试
  或真实 Case；不再向 Owner 重复呈报或询问。
- 仅存的纪律：**不主动读取、不打印、不在聊天/日志/报告/receipt 中显示凭据具体值**；
  仅当凭证实际无效导致 Provider 无法调用时报告执行故障。
- 内部交付包可按 Owner 既有要求包含运行所需 kth-env。

## 5. 语言与提交

- commit 信息、代码注释、文档一律优先简体中文。
- 提交为本地 commit；不 push、不建远端；回导 sunny-skills 仅凭 Owner 指令以单一受审
  快照执行。

## 6. 停止条件（M1 起适用，源自 Owner 指令与 v1.1 §11）

- wheel 身份不符；attached source 不是 80/81；微玖核心附件/RawCapture 缺失或损坏；
- 需要模型自行发明 KTH criterion；需要从旧漂移源导入业务代码；需要创建 allowlist 外
  文件；发现原版业务规则无法机械定位。
- 凭证、密钥轮换、Git 历史、内部白名单**不属于**停止条件。

## 7. 里程碑硬门（Owner 2026-09-06 补充）

- M0 审核通过后不直接进入 M1；强制先行 **M0.5：端到端 Walking Skeleton 可行性证明**
  （一个实现段内、3–5 工作日量级、最小代码）。
- M0.5 两条实测链：链 A 原版完整 fixture 经新接口跑通
  `六维 → maturity → 政策 → D1 → D2 → 投研报告 → CEO Brief → 新验证器`（禁止复制
  fixture 预期输出充当结果）；链 B 微玖真实 RawCapture 经
  `哈希重算 → CaptureReceipt 验证 → source-policy 验证 → 资格化 → Evidence Store 写入
  → 至少一个真实 criterion 消费`（保留字节区间/精确定位符）。
- M0.5 恢复证明：在 evidence_ready → dimensions 边界强杀一次；重启后校验既有输出、
  不重复导入 RawCapture、不重复调用 Provider、从最后完成阶段继续、业务结果一致。
- M0.5 立即失败条件（任一即停，不继续堆代码）：六维业务函数无法脱离旧 session/Gate
  调用；必须复制修改大量未知业务逻辑才能出结果；D1/D2/报告依赖旧 publication-authority
  拼接；原版 fixture 无法确定性重放；微玖 RawCapture 无法验证；需要放宽 validator；
  需要模型发明 Evidence 或 criterion。
- M0.5 额外禁令：不得调用/迁移 rev744 session、不得用 family-private Gate、action
  rekey、Decision B、monkeypatch、bypass validator、手写六维结果/maturity/D1/D2/报告、
  系统失败转业务 NO、以 fixture 通过冒充微玖正式 Case 完成。
- M0.5 交付（一次性）：实际命令与退出状态、生成文件清单、微玖 Evidence 入口证明、
  恢复前后结果对比、旧 staged 依赖扫描、手工业务构造扫描、PASS/FAIL 结论、是否建议
  继续完整项目；经 GPT-5.6-sol fresh-context 审核后由 Owner 决定是否批准 M1。
