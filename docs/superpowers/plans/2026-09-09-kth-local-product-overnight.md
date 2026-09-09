# KTH 本地产品闭环通宵实现计划

> **执行要求：** 按本计划在同一持续 Goal 中逐任务执行；每个行为变更先建立失败测试，再做最小实现。完成内部关卡后直接进入下一任务，不等待 Owner 确认。

**目标：** 在 2026-09-10 08:00（Asia/Shanghai）前交付一个可实际操作、可中断恢复、默认不调用外部 Provider 的本地 Plugin 候选，并冻结可供独立验收的交接证据。

**架构：** 保留既有 `CaseStore`、`BlobStore`、`Journal`、资格、六维 runner、manifest 与 trace。新增单一工作流编排层和受控复核队列；CLI 与 Plugin 薄入口只调用该编排层。许可和聚合 profile 都使用版本化、内容寻址合同，不从自由文本或调用方结果反推权限。

**技术栈：** Python 3.12、SQLite、标准库 argparse/json/hashlib/unicodedata、PyPDF2、python-docx、pytest。

---

### 任务 1：关闭角色等价赋值边界

**文件：**
- 修改：`plugin/src/kth_hybrid/roles.py`
- 新建测试：`plugin/tests/test_product_overnight_roles.py`

- [ ] 新增失败测试：全角 `：`/`＝`、JSON 转义键、嵌套对象/数组、PRO/CON/CHAIR、round、confirmation。
- [ ] 新增正例：中性字段名提及和原始文本逐字保留。
- [ ] 新增长度 65536、结构深度 16 的边界测试，超限必须明确拒绝且不得递归崩溃。
- [ ] 运行新测试，确认只因当前缺少 NFKC/有界 JSON 检查而失败。
- [ ] 在 `_scan_authority` 中增加只读 NFKC 验证视图、有界完整 JSON 解码及决定键 canonical token；禁止 `eval`，不改原始对象与摘要。
- [ ] 运行新测试、审核方脚本、旧角色/TRL/TMRL 窄回归。
- [ ] 中文提交测试和实现，分别保留红灯与绿灯证据。

### 任务 2：关闭用途许可 P2

**文件：**
- 新建：`plugin/src/kth_hybrid/evidence_permissions.py`
- 修改：`plugin/src/kth_hybrid/aggregate.py`
- 修改：`plugin/src/kth_hybrid/roles.py`
- 修改：`plugin/src/kth_hybrid/store.py`
- 新建测试：`plugin/tests/test_product_overnight_permissions.py`

- [ ] 新增失败测试：同 evidence class 跨 criterion、资格用途不匹配、空/扩大 support scope、candidate/confirmation 自洽但与原许可不一致。
- [ ] 用真实 `CaseStore -> qualification -> dimension runner -> manifest/view` 合成正例，禁止只手写 view。
- [ ] 定义 `evidence-use-license.v2`：正文冻结 review、criterion、资格 view、原始 `allowed_uses`、精确 `allowed_criterion_uses`、subject/scope/result；ID 覆盖完整正文。
- [ ] 新 view 生成 v2 许可；旧许可仍可只读验证，但角色消费时进入明确的“旧合同限制”拒绝，不静默扩权。
- [ ] 角色 `review_target` 与 confirmation 增加 `requested_use`，必须同时匹配许可的 criterion、use、support scope 和证据引用。
- [ ] 下游 review 保留 `license_id`/`requested_use`，使冻结结果可追到许可来源。
- [ ] 运行许可测试及资格、角色、六维回归，中文提交。

### 任务 3：登记命名 aggregation profile

**文件：**
- 新建：`plugin/src/kth_hybrid/aggregation_profiles.py`
- 修改：`plugin/src/kth_hybrid/aggregate.py`
- 新建测试：`plugin/tests/test_product_overnight_profiles.py`

- [ ] 新增失败测试：未登记 profile、漏维、混 CaseBasis/scope、未知规则或结果 schema、跨评估单元、调用方结果反推 profile、latest 替换旧结果。
- [ ] 从当前批准代码常量登记一个显式命名 profile，固定六维规则版本、结果 schema 和 catalog 身份；不得由输入结果构造 registry。
- [ ] 新 manifest schema 冻结 `profile_id/profile_digest` 与六个显式 result ID；生成时必须显式提供 profile 名。
- [ ] 保留 v1 manifest 的只读历史重放；v1 不能生成新 profile 权限，也不能被 latest 替换。
- [ ] 运行 profile、aggregate、manifest/trace 回归，中文提交。

### 任务 4：建立持久化工作流与受控复核队列

**文件：**
- 新建：`plugin/src/kth_hybrid/review_queue.py`
- 新建：`plugin/src/kth_hybrid/workflow.py`
- 修改：`plugin/src/kth_hybrid/journal.py`
- 修改：`plugin/src/kth_hybrid/intake.py`
- 修改：`plugin/src/kth_hybrid/store.py`
- 新建测试：`plugin/tests/test_product_overnight_workflow.py`

- [ ] 新增失败测试：创建 job、附件幂等导入、投影持久化、扫描 PDF/损坏/不支持格式状态、awaiting 路径零 Provider 调用。
- [ ] 新增失败测试：review request 缺引用、错 quote/job/scope/unit/profile/use、过期输入身份、越权结果字段和伪 source mode 全部拒绝。
- [ ] 新增失败测试：合法 `manual_import` 响应先封存、后消费；模式在 job、响应与导出中持续保留，不冒充自动复核。
- [ ] 工作流 job ID 冻结原件集合、投影身份、CaseBasis、assessment unit、profile/method、复核请求输入；任一变化生成新身份。
- [ ] `Journal` 继续作为任务认领与 fencing 权威；review queue 明确 `awaiting_authorized_analysis`、`response_sealed`、`consumed`，不得用业务不足或失败代替。
- [ ] `review_request.v1`/`review_response.v1` 采用内容寻址 ID，响应禁止写 level、native disposition、最终决定。
- [ ] 附件导入先完整验证再登记；同一原件不重复业务对象。投影正文、工具版本、定位与未处理原因持久化。
- [ ] 运行工作流/底层存储/恢复回归，中文提交。

### 任务 5：统一 CLI 与 Plugin 候选入口

**文件：**
- 修改：`plugin/src/kth_hybrid/cli.py`
- 修改：`plugin/pyproject.toml`
- 新建：`plugin/.codex-plugin/plugin.json`
- 新建：`plugin/commands/kth-local.md`
- 新建测试：`plugin/tests/test_product_overnight_cli.py`

- [ ] 新增子进程失败测试，覆盖 `case create`、`intake add`、`status`、`run`、`resume --job-id`、`review list/import`、`trace`、`export`。
- [ ] console script 统一为 `kth-local = kth_hybrid.cli:main`；Plugin 只提供一个调用同一 CLI 的薄入口。
- [ ] `run` 默认离线，遇专业分析停在 awaiting；模拟必须显式选择，正常模式不得调用 `CountingSimulatedProvider`。
- [ ] `trace` 按精确 ID 路由来源定位、criterion、CRL、非 CRL、manifest/view；`export` 只导出核验包，不使用报告/Brief 命名。
- [ ] 错误路径返回稳定非零退出码和清晰中文状态；resume 必须指定 job，不选 latest。
- [ ] 运行 CLI 子进程、打包元数据和 Plugin 结构测试，中文提交。

### 任务 6：中断恢复、真实材料演示与合成全链

**文件：**
- 新建测试：`plugin/tests/test_product_overnight_recovery.py`
- 外部工件：`D:/t/kth-product-local-overnight-20260910/run-20260909-2345/`

- [ ] 三个独立新 Case 用真实子进程硬退出：原件持久后、响应封存后、维度完成而 manifest 前。
- [ ] 第二进程用显式 job ID 恢复；记录退出码、前后状态、孤立工件、输出 ID 和重复执行对比。
- [ ] 验证同输入幂等、输入变化新 job、`outcome_unknown` 不自动重派；明确该证明不等于外部 exactly-once。
- [ ] 从已授权 night-fix3 或审核包中选择明确可用的一份附件，经 CLI 实际导入、投影、状态、追溯并停在 awaiting；不扫描整机。
- [ ] 用明确标记合成 Case 导入合法 manual/simulated 返回，真实调用既有资格、六维 runner、命名 profile、manifest/view/trace；真实材料和合成目录分离。
- [ ] 导出核验包并逐文件 hash 重建。

### 任务 7：全套、保护核验和冻结交接

**文件：**
- 修改：`README.md`
- 追加：`AGENTS.md`
- 新建：`docs/checkpoints/KTH-LOCAL-PRODUCT-OVERNIGHT-20260910.md`
- 外部交接：`D:/t/kth-product-local-overnight-20260910/.../handoff/`

- [ ] 运行新增反例、审核脚本、原完整全套和所有真实门控，逐项记录命令、环境、stdout/stderr、退出码和 SHA256；任何测试集有 failed/skipped 均不得写“通过”。
- [ ] 复制 night-fix3 到新候选目录，保存复制前后全量清单；不得修改旧候选、旧 Case 或旧 session。
- [ ] 核对全部历史保护 hash 和 night-fix3 前后清单。
- [ ] 生成 `handoff.json`、`产品能力实况表.md`、`明早验收命令.md`、`Owner单次决策表.md`、`明早独立验收入口.md`、最终快照与文件 hash 清单。
- [ ] 07:00 后不增加功能；07:45 前冻结最后候选；最终文档指向最后一次通过验证的代码提交。
- [ ] 中文交接提交；不 push/merge/tag/release，不声明独立验收通过。

