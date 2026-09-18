# 开发文档导航

采用规格驱动开发：需求 → 工作包/接口 → 实现 → 验收证据 → 交付。只维护一条产品主线，不额外引入Spec工具框架。

## 1. 默认阅读

先读[AGENTS](D:/UGit/kth-irl-hybrid/AGENTS.md)，再按下表接续。除此之外没有“全历史必读”要求。

| 文档 | 唯一职责 |
|---|---|
| [CURRENT](D:/UGit/kth-irl-hybrid/docs/CURRENT.md) | 当前状态、实际权限和下一动作 |
| [AG1-SPEC](D:/UGit/kth-irl-hybrid/docs/specs/agent-evaluator-delivery-v1.md) | 产品目标、范围、需求和完成定义 |
| [AG1实施计划](D:/UGit/kth-irl-hybrid/docs/plans/AG1/交付实施与验收计划.md) | 当前包接口细化、文件/来源、验收场景 |
| [AG1恢复输入](D:/UGit/kth-irl-hybrid/docs/plans/AG1/新执行对话完整输入.md) | 新对话恢复方法，不复写上述合同和进度 |

## 2. 只有具体任务需要时才读

| 保留材料 | 当前用途，不是保留整套旧路线 |
|---|---|
| [方法范围](D:/UGit/kth-irl-hybrid/docs/specs/method-scope-v1.md) | AR-09/M4仍沿用180条目录、CRL范围及各维N/A边界 |
| `specs/r2a-crl-rule-spec.md`、`r2b-frl-rule-spec.md`、`r2-rule-coverage.json` | M4核对现有CRL/FRL内核的输入、累计行为和准则出处；其中历史候选状态不作当前进度 |
| [方法分歧台账](D:/UGit/kth-irl-hybrid/docs/divergence-ledger.md) | 方法问题的已裁定依据与新分歧记录，不恢复旧阶段任务 |
| [未冻结政策草案](D:/UGit/kth-irl-hybrid/docs/policy/org-d1-policy-draft-v0.md) | M5识别未决政策问题，不是已采用的政策；旧M3阻塞条件不套用AG1 |
| [资料入口](D:/UGit/kth-irl-hybrid/docs/reference/README.md) | M2/M3查原始输入、聊天依据、来源hash和本仓位置 |
| `M0-*.md`六份文件 | 是`provenance/SHA256SUMS`固定的历史校验对象，不是现行开发文档，不默认加载；仅来源/不变量问题需要时定位 |
| CURRENT中具名的既有独立裁决 | 说明仍复用底座的验收范围及未决风险，不用重复读取全部审核过程 |

## 3. 已退出开发路径

旧阶段计划、审核流水、旧R1子集合同、旧运行层路线、已更正的首次资料分析，以及旧版AGENTS中的历次授权，统一见[历史归档](D:/UGit/kth-irl-hybrid/docs/archive/README.md)。

归档不参与默认上下文、不继续维护、不授予权限。只有能说清“本任务依赖哪项旧结论/源码出处/未决风险”时才读取具体文件；不能遇到不确定就重新通读全部历史。

真实原件、Case、凭据、日志和备份不属于本轮清理范围。

## 4. 维护方式

- 每项代码变化对应需求和验收；工程细化写回当前包，不另建总体路线。
- 实际进度只写CURRENT；计划用稳定工作项ID定义任务，不另维护勾选进度，不在AGENTS、恢复输入和README重复同步。
- 启动核对、小步检查点、受影响验收待复验和交接检查统一按[AG1计划第6节](D:/UGit/kth-irl-hybrid/docs/plans/AG1/交付实施与验收计划.md#6-跨对话检查点与防偏航)执行；CURRENT是待核实的状态索引，不代替实际代码和证据。
- 不为这些执行纪律另建进度脚本、hook或管理平台；由执行者落实、独立审核核对。不以文字规则承诺自动保证。
- 不改变产品范围的修复不升级为新规划；方法/权限/验收变化须Owner采用。
- 历史事实保持原字节；当前文本按语义去掉已废止流程，不因历史hash保留就继续执行。

## 5. 本轮来源与范围

2026-09-18，Owner明确要求按与当前方向的关联精简，而非只比较字节。本轮自有编辑范围为AGENTS、CURRENT、本索引、资料入口、AG1恢复输入及Spec归档引用；三份退役文档移动归档，旧版快照和理由记在[语义精简清单](D:/UGit/kth-irl-hybrid/docs/archive/20260918-semantic-cleanup/manifest.csv)。

不改产品、六维规则、政策草案、来源CSV、M0历史manifest或既有裁决；不提交、不推送。真实执行状态仍只看CURRENT。

后续Owner要求增强长期工程执行约束，并明确不保留非必要管理工具；相应自有调整仅AGENTS、CURRENT、本导航、AG1计划和恢复输入。不改Spec业务目标与验收矩阵，具体状态仍只看CURRENT。
