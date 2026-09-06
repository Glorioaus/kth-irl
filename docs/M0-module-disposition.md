# M0 逐模块处置矩阵

日期：2026-09-06　|　基线：批准 wheel（81 模块，70,709 行）+ 插件侧（scripts 12 + tools 2，
6,981 行）　|　行数为 wheel/源树实测。

四类口径：
- **直接复用**：字节级复制（仅数据/材料，经 M1 审计导入）；
- **抽取后复用**：业务语义抽入新包 `kth_hybrid`，每项配差分/coverage 测试证明等价；
- **oracle 重写**：不移植代码，按冻结规格重写，以原版测试与 wheel 行为对照；
- **废弃**：不进入新系统（旧机械）；标注“M1 指针核查”者允许在机械定位到业务语义时
  将该函数移入抽取清单，其余部分仍废弃。

## A. 数据与材料（直接复用）

| 资产 | 内容 | 处置 |
|---|---|---|
| KTH 三件材料 | Compiled F PDF / User Guide / Dashboard XLSX | 直接复用（解释性权威，哈希见 baseline） |
| 七 Registry v1 | baseline / crl-issue-family / controller-method / product-class-routing / research-dimension-routing / source-policy / terminal-value-axis | M1 逐条审核后升 v2 导入；baseline 判据显式化按机械提取合同 |
| demo_registry.json | demo 模式注册表 | 直接复用（测试夹具） |

## B. wheel 81 模块

### B1 抽取后复用（业务内核，39 个）

| 模块 | 行数 | 去向 |
|---|---|---|
| v1/crl_vertical.py | 1304 | CRL 判据内核 |
| v1/crl_first_principles.py | 1134 | CRL 第一性原理 |
| v1/crl_specialist.py | 1110 | CRL 角色语义 |
| v1/crl_intake.py | 658 | CRL intake 语义 |
| v1/crl_community.py | 644 | CRL 社区证据 |
| v1/brl_vertical.py | 1170 | BRL 判据内核 |
| v1/trl_vertical.py | 787 | TRL 判据内核 |
| v1/tmrl_vertical.py | 1419 | TMRL 判据内核 |
| v1/tmrl_professional_runtime.py | 1415 | TMRL 运行语义 |
| v1/iprl_vertical.py | 956 | IPRL 判据内核 |
| v1/iprl_competition.py | 1260 | IPRL 竞争分析 |
| v1/iprl_predispatch.py | 1042 | IPRL 预派发 |
| v1/iprl_patent_discovery.py | 849 | IPRL 专利发现 |
| v1/frl_vertical.py | 461 | FRL 判据内核 |
| v1/frl_professional_runtime.py | 733 | FRL 运行语义 |
| v1/active_crl_brl_runtime.py | 1373 | CRL/BRL 主动运行 |
| v1/active_trl_tmrl_runtime.py | 1485 | TRL/TMRL 主动运行 |
| v1/active_iprl_runtime.py | 728 | IPRL 主动运行 |
| v1/active_frl_runtime.py | 742 | FRL 主动运行 |
| v1/existing_dimension_professional_runtime.py | 800 | 维度专业运行 |
| v1/dimension_integration.py | 848 | 六维装配语义 |
| v1/dimension_reasoning_control.py | 801 | 辩论控制语义 |
| v1/dimension_role_projection.py | 741 | 角色投影 |
| v1/acquisition.py | 1367 | 捕获使命编译 |
| v1/active_evidence.py | 438 | Evidence↔RawCapture 绑定与资格化 |
| v1/active_retrieval.py | 630 | 主动检索语义 |
| v1/research_dispatch.py | 881 | 研究调度（状态改接 mission 状态机） |
| v1/research_frames.py | 398 | 研究框架 |
| v1/document_extraction.py | 233 | 带定位符文档解释 |
| v1/capsule.py | 72 | 证据胶囊 |
| v1/subject_identity.py | 882 | 身份验证计划 |
| v1/identity_adapters.py | 304 | 身份适配 |
| v1/identity_research_reporting.py | 477 | 身份研究报告 |
| v1/web_adapters.py | 382 | Web Provider 适配（接 mission 状态机） |
| v1/patent_adapters.py | 608 | 专利 Provider 适配（接 mission 状态机） |
| v1/community_evidence.py | 740 | 社区证据规则 |
| v1/community_adapters.py | 335 | 社区证据适配 |
| v1/controller.py | 980 | 控制器阶段语义（映射 controller-method registry） |
| v1/terminal_value_workflow.py | 1812 | 终值轴工作流 |
| v1/branch_first_principles_runtime.py | 1030 | 分支第一性原理 |
| v1/branch_census_runtime.py | 823 | 分支普查 |
| v1/branch_runtime_projection.py | 276 | 分支投影 |
| v1/cross_dimension_decision.py | 1799 | D1 fail-closed 语义 |
| v1/decision_authority.py | 1608 | A7/PolicyChoice 语义（去拼接） |
| v1/decision_reasoning_control.py | 722 | 决策推理控制/角色隔离 |
| v1/d2_projections.py | 1626 | D2 语义（门控关系待 Q1 机械定位） |
| v1/investment_report.py | 1235 | 投研报告编译 |
| v1/ceo_decision_brief.py | 1376 | CEO Brief 编译 |

（上表 49 行含分支族与报告族；B1 合计 49 模块，约 43,600 行。）

### B2 oracle 重写（基础设施与验证器，17 个）

| 模块 | 行数 | 说明 |
|---|---|---|
| v1/local_whole_chain.py（拆解） | 10259 | 正式链验证语义以测试为 oracle 重写；旧 session/revision 编排部分废弃 |
| v1/reliability.py | 1531 | 验证器语义 oracle |
| v1/semantic_control_contract.py | 726 | 语义完整性合同 oracle |
| v1/local_semantic_integrity.py | 547 | 同上 |
| v1/tool_integrity.py | 289 | fail-closed 工具执行 |
| v1/dimension_ports.py | 223 | fail-closed 端口概念重实现 |
| v1/provider_host.py | 1217 | Provider 宿主（薄基础设施，接 mission 状态机） |
| v1/provider_conformance.py | 257 | Provider 一致性 |
| v1/model_gateway.py | 508 | 部署差异模块；新网关 oracle 重写 |
| v1/model_runtime.py | 154 | 模型运行时 |
| v1/credential_runtime.py | 144 | 凭据运行时（redact 优先） |
| v1/retrieval_index.py | 949 | 平面索引替代 |
| v1/local_authority.py | 294 | 新 provenance/ProofBundle |
| v1/kth_baseline_authority.py | 103 | 基线权威 → 新 baseline 钉 |
| v1/release_readiness.py | 158 | 发布就绪 |
| v1/non_real_calibration.py | 97 | 校准夹具 |
| v1/registry.py（loader） | 16 | v2 loader 重写 |
| v1/provider_runtime.py（拆解） | 658 | “搜索≠证据”规则语义抽取（并入 B1 合同）；传输执行 oracle 重写 |
| cli.py | 1188 | 新 CLI 面 |
| delivery.py | 1022 | 交付管线 |
| evaluator.py | 113 | demo 有界求值器（测试用） |

### B3 废弃（旧机械，12 个）

| 模块 | 行数 | 说明 |
|---|---|---|
| v1/case_run_ledger.py | 524 | revision/action 账本 → 7 阶段生命周期替代 |
| v1/case_runtime.py | 818 | 旧 case 状态机（intake 语义如有内嵌，M1 指针核查） |
| v1/case_execution.py | 481 | 旧执行编排 |
| v1/case_foundation.py（拆解） | 691 | intake/附件登记业务语义抽取入 B1；旧 foundation 状态废弃 |
| v1/local_composition_root.py | 1931 | 旧 session 装配 |
| v1/local_runtime.py | 474 | 旧本地运行装配 |
| v1/active_case_orchestration.py | 378 | 旧 staged 编排 |
| v1/d2_migration.py | 131 | 旧机械迁移 |
| v1/qdrant_retrieval.py | 383 | YAGNI：单 Case 规模由平面索引替代 |
| v1/__init__.py、__init__.py、__main__.py | 641+303+7 | 包胶水，新包自建 |

## C. 插件侧（wrapper）

| 文件 | 行数 | 处置 |
|---|---|---|
| scripts/compile_complete_report.py | 326 | 抽取后复用（机械合并语义） |
| scripts/verify_payload.py | 243 | oracle 重写 → baseline/provenance 校验 |
| scripts/kth_irl_runtime_provenance.py | 272 | oracle 重写 → 简单版本钉 |
| scripts/kth_irl_launcher.py | 212 | 废弃（新入口） |
| scripts/natural_language_entry.py | 972 | 废弃（新 intake） |
| scripts/kth_irl_active_output_transaction.py | 1379 | 废弃（publication v2 拼接） |
| scripts/kth_irl_staged_{dimension,crl_brl,trl_tmrl,iprl,frl}_adapter.py | 3030 | 废弃（5 个暂存适配器） |
| scripts/bootstrap_runtime.py | 366 | 废弃（标准安装替代） |
| tools/assemble_from_source.py、tools/verify_fresh_checkout.py | 181 | 废弃（新基线校验替代） |

## D. 测试、文档、技能

| 资产 | 处置 |
|---|---|
| 原版测试 20 文件（248 测试函数） | oracle 语料：M1 审计导入 reference/tests 作对照运行，不直接充当新测试 |
| references/ 14 份治理文档 | 只读历史参照；`kth-env.example` 复用为 deploy 模板；其余不入新插件 |
| skills/ 14 个技能 | 业务提示词资产：M3/M4 经审核改写复用（YES/NO Advocate、Chair、报告技能） |
| 旧 worktree receipts/gates/wheel 构建器/family-private 脚本 | 全部废弃，永不迁移 |

## E. 矩阵纪律

- 本矩阵是 M1 导入与实现范围的唯一授权表；处置变更 = 修订本文件 + 差异审核记录。
- 标注“M1 指针核查/拆解”的 5 个模块（local_whole_chain、provider_runtime、case_foundation、
  case_runtime、及 B1 各抽取项内嵌的旧状态残留）在 M1 机械定位后出补充裁定，禁止凭
  印象扩大抽取范围。
