# R2-A 检查点（2026-09-08）

范围：批准 wheel 中现有180条规则的覆盖清单，以及 CRL 13条、1–4级完整离线维度切片。
未进入其他维度、专业角色、联网补采、真实模型/Provider、D1/D2、报告或Plugin发布。
本文件为执行方自检，未声称独立审核通过。

## 基线与提交

- R1验收基线：`5831a0556e7d490ad31f4110d9c8b87f05041cb2`。
- 初始CRL实现：`5b5bbd2 实现(R2-A)：CRL离线规则与维度切片`。
- 证据门、原子发布与测试收口：`7d42a6b 修复(R2-A)：收紧CRL维度证据门与原子发布`。
- 实际导入：`D:\UGit\kth-irl-hybrid\plugin\src\kth_hybrid\__init__.py`。
- 当前工作Case：`D:\t\kth-rebuild-cases\weijiu-r2a`；R1至R1.6 Case与旧session只读。

## 交付能力

- `r2-rule-coverage.json`机械登记180条：CRL13已实现，BRL36、TRL25、TMRL38、IPRL32、
  FRL36共167条明确为后续未实现；原版单准则测试无法机械定位时保持null并标记缺口。
- `kernels/crl.py`为13条准则定义独立 findings 合同；无复核为产品`insufficient`/原生null，
  受控反证为`not_met`，支持与反证冲突为`partial`，满足准则特定条件才为`met`。
- 累计按低到高逐级全满足；不计算平均分，高级结果不能越过低级缺口，CRL5–9不存在。
- CRL review绑定CaseBasis版本、Claim、准则、摘录hash、subject_scope、findings、reviewer与范围。
- 维度入口重核R1原件、定位/投影、摘录hash、Claim内容、资格、CaseBasis证明和review绑定。
  已登记review任一链路失效时，整个维度候选为`execution_failed`，不暴露旧met。
- 维度结果先内容寻址落盘，再在`BEGIN IMMEDIATE`事务中发布SQLite历史记录；固定JSON仅为派生
  最新视图。同输入重放复用同一result_id，历史结果不被覆盖。
- `trace_crl_dimension`读时重核结果blob、冻结摘要、CaseBasis、Claim、Source、Qualification和
  CRL review。

## 测试与真实结果

- 13条逐准则最小正例及缺finding反例；累计低级阻断；`not_met`、`partial`、联系人去重与
  多review合并均有测试。
- 入口反例：无原件、Claim解释篡改、错scope均先复现；修复后分别形成`execution_failed`或
  明确拒绝，输入身份变化可见。
- R2-A专项及真实门控：`27 passed / 0 failed / 0 skipped，exit 0`。
- 最终全套（含R1.6与R2-A真实门控）：`256 passed / 0 failed / 0 skipped，exit 0`；唯一warning
  为zip重复成员拒绝测试的Python UserWarning，测试本身通过。

真实CRL结果：仅GOV合格窄主张经`CRL-R2A-GOV-C1`复核支持`CRL1-C1=met`；其余12条没有
准则绑定的合格findings，保持`insufficient`。累计等级未形成，首个未满足级为1。该结果不要求
微玖达到任何等级，不将政府页扩大为客户反馈、分群、角色、重要性或定位证据。

## 方法与限制

批准wheel的CRL编译器由宿主提供adjudication；本批实现受控离线findings合同，未证明与原宿主
或官方KTH方法全等价。CRL仅1–4级，非官方KTH评估。主体仍为claimed；BP缺归属和可靠时间，
公司报道时间定位不一致；未提取Source实例和来源独立性仍未完成。

最终L0-L4、日志及hash见`R2-A-handoff.json`。完成后停止送审，不自动推进其他维度。
