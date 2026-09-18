# 工程决定：整体重建运行层（runtime-v3）

日期：2026-09-08。分支：`codex/runtime-rebuild`（自 main `23f2d9a` 创建）。
性质：工程路线决定记录，不是业务方法批准，不是正式D1授权。

## 1. 本次授权依据

Owner 于 2026-09-08 批准采用《KTH整体重建执行计划v3》（2026-09-07，位于
`D:\t\kth-implementation-plan-20260907\KTH整体重建执行计划-v3.md`），**仅授权 R0＋R1**：
T01–T06，以及 v3 明确新增的 I/O 历史交接、audit/trace、证据 census 和 R1 交接工件。
开工指令原文：`D:\t\kth-implementation-plan-20260907\发给GLM的R0-R1开工指令-v3.md`。

授权明确不包含：微玖进入尽调、金额/估值/付款/签约/交易条款、正式 D1/投研报告、
R2（T07–T15）、merge/push/tag/release、远端配置、回导主仓。

## 2. 本决定取代什么（仅限 R0＋R1 执行期）

| 旧约束 | 新规则（v3） |
|---|---|
| Hybrid v1.1 批量模块抽取（48/21/12 模块矩阵） | 不按模块数量分配任务；按业务能力增量交付，仅复用当前确实需要的函数/模块 |
| M0-only 写入限制（M0 审核通过前不写业务代码） | 已解除：本仓 `plugin\` 目录自 T02 起按工作包创建代码与测试 |
| 强制 M0.5 Walking Skeleton（原验收口径） | 被 R1 取代：真实证据存量表、核验视图、trace、恢复记录（v3 §4） |
| 第一轮旧裁决"C/薄入口优先、原版完整D1/D2接口尽量沿用" | 整体重建运行层、schema、编排、恢复、决策准入及报告流水线；旧七万行系统不是改造宿主 |
| 旧 29–46 人日排期 | 不作为当前开工约束；R1 记录实际耗时，R2 按实测重估 |
| "先证明原版整套工件可串接再开工" | 直接建设新产品最小合同，按业务能力增量交付 |

## 3. 本决定不改变什么

- **原始资产保护**：主仓 `D:\UGit\sunny-skills`、旧 worktree、旧 stage runtime、
  family-runs、rev744 session（`D:\t\kth-phase8-real-session-...\session`）、未批准快照
  `kth-0.15-0828` 一律只读。
- **凭证纪律**：不读取、不显示凭证具体值；凭证治理为非阻断 backlog。
- **业务不变量**（v3 §1.2 全文有效）：真实原件不改写；证据必须有来源定位；来源/身份/
  时间不合格不得提升成熟度；搜索零结果不证明事实不存在；无合格证据不得 met；六维不可
  由总分替代；正式D1仅YES/NO；系统或方法执行失败不得成为业务NO；YES仅批准进入尽调；
  外部动作可审计，未知结果不盲重发，失败永久留存。
- **方法边界**：不重新发明 criterion，不篡改成熟度含义。180 条只是批准 wheel 当前实现
  的条目；CRL 仅 1–4 级；CRL 无原生 insufficient；FRL 受限 N/A。业务语义变化单列，
  不能由工程开工授权冒充方法批准。
- **M0 历史保留**：`baseline\`、`provenance\`、既有 docs 不改写；本文件为追加决定。

## 4. 权威基线（继承 M0 钉定并本轮复核）

| 项 | 值 | 本轮复核 |
|---|---|---|
| 批准 wheel | `D:\UGit\sunny-skills\plugins\kth-irl-evaluator\runtime\kth_irl_evaluator-0.1.5-py3-none-any.whl` | 2026-09-08 sha256 复核一致 |
| wheel SHA-256 | `2c49050858555ebb063a7b82177ee43e7caf7d2b93e2319998d7d0b047a471fe` | 一致 |
| 主仓源码解释基线 | `...\kth-irl-evaluator-v0.1.5-it-development\src`，81 模块、80/81 一致、唯一 `model_gateway.py` 部署差异 | 沿用 M0 核验，未重验 |
| 原始 session | revision=744、phase=dimension_set、formal_admission=false、session_hash `a93e446f…75965` | 只读，不迁移状态 |
| 微玖资产 | 80 条捕获清单（40 非空正文/40 零字节）、清单 18 种正文 hash、2 附件、2 abandonment、bookkeeping | R1 按实际重新分层盘点，不硬凑旧数字 |
| 原版测试基线 | 78 项：56 通过、22 错误、0 跳过；22 个错误均在目录替换调用栈出现 WinError5 | 因果范围见 `D:\t\kth-implementation-plan-20260907\Windows-IO历史交接.md`，不得称全绿也不称业务断言失败 |

## 5. 写入范围（R0＋R1）

- 实施仓库：`D:\UGit\kth-irl-hybrid`，分支 `codex/runtime-rebuild`；本地提交，不 push。
- 真实 Case 工作根：`D:\t\kth-rebuild-cases\<case_id>`（仓库外）。
- 首批文件：T01–T06 计划文件 ＋ 必要的项目环境/忽略规则/R1 检查点文件，逐项记录理由；
  不一次性建满空目录。
- 唯一素材导入例外：按 M0 清单读取两附件与 80 条捕获，捕获原文及其来源/传输/资格依赖，
  复制到新 Case 并生成诚实 ImportRecord；原文与旧 receipt 字节不改；旧 qualification 仅
  为候选，不继承权威。

## 6. 网络与诚信红线（首批）

零网络、零真实 Provider/运行时模型 API、不加载或打印真实凭证、不联网安装依赖。
禁止手填六维结果/成熟度/决定凑 schema；禁止 pipeline 失败变业务 NO；禁止 hash 证明
现实真实性；禁止素材数量当 Evidence 数量；禁止本地 write-ahead 日志称为无条件外部
exactly-once。必须保留原始失败、每次 attempt、输入身份和恢复过程。

## 7. 首批完成判据（v3 §6）

完整 PASS 要求：实际功能、全量分层盘点、至少一个真实合格窄主张的判据消费、trace、
困难边界（CRL/FRL/TMRL）和恢复均有实证。若真实样本只形成 Gap，则部分通过；不是为了
通过而降低资格。完成后停止送审，禁止自动 R2。
