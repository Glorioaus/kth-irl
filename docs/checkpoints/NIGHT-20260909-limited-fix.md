# NIGHT-20260909 限定整改检查点

日期：2026-09-09。状态：执行方限定整改候选已完成，等待fresh-context独立复验。
本文件不声称独立验收通过，不授权网络、真实模型/Provider、正式D1/D2、报告、投资决定或发布。

## 基线与提交

- 起始HEAD：`28f335eb834c9a472832fd89d2a660adb418610f`，工作树干净。
- 实现提交：`fc1f8d72785afc64715c643ffc78774355d58f0d`。
- manifest版本配对测试提交：`8f5354efa885d377c131792077c5ef640e3dfa41`。
- 状态/时间纪律修正：`ad8378288dc94a4606172d584b9465f03caa4535`。
- 最后全绿代码HEAD：`8f5354e`；`ad83782`仅修改文档与覆盖状态。

## 分包结果

### 结果trace与版本

- 非CRL结果合同升为`kth-hybrid.dimension-result.v2`，合同版本进入冻结摘要并生成新结果ID。
- `validate_dimension_payload`按冻结的`dimension_id + rule_version`选择唯一确定性evaluator，
  重跑完整dimension并精确比较；未知版本不使用latest降级。
- trace精确绑定SQLite行的dimension、CaseBasis版本、scope、scope_id、product_status、result/input
  身份；校验schema、批准wheel hash、criteria集合/排序和逐准则rule_version。
- scope_id重绑、数据库状态/版本重绑、结果blob改成全met/9级、未知rule version均被自动反例拒绝。
- FRL规则内核`kth-hybrid.frl.r2b.v1`原样保留，仅使用新结果合同和共享trace。

### 六维manifest

- 新增`kth-hybrid.aggregation-manifest.v1`。调用方必须显式给出六个精确result_id；产品代码不再
  自动选择数据库latest。
- manifest冻结每维result/input、CaseBasis/主体、评估单元或融资主体、catalog、rule version、
  result schema、产品状态和trace状态。
- 六维视图升为`kth-hybrid.offline-six-dimension-view.v2`，只消费manifest指定结果；同Case新增
  第二BRL单元结果后，旧manifest仍重放原BRL结果。
- 实际证据绑定生成内容寻址`EVIDUSE::*`许可，供离线角色引用。

### 四维canonical语义

- BRL升`kth-hybrid.brl.night.v2`：BRL5/6/7交易证据统一执行wheel commitment强度；软意向、
  可退款弱承诺、未履约pilot/test sale不支持BRL6；BRL7仍要求已交付商业销售和两个去重客户；
  BRL8/9经营指标要求期间、分母、实际值/目标值及达标比较。
- TRL升`kth-hybrid.trl.night.v2`：按证据类区分概念、主动研发、需求、实验室、相关环境、运行
  环境、实际运行、纵向运行、制造和持续改进；TRL3-C2主动研发无需伪造试验结果，也不外溢到
  TRL3-C1。
- IPRL升`kth-hybrid.iprl.night.v2`：官方申请/授权/维持记录、已执行协议、专业分析分别强制；
  8-C2/9-C2要求在权状态，9-C2还要求多个业务地域及维持核验。
- TMRL升`kth-hybrid.tmrl.night.v2`：38条使用显式语义requirement和证据类record-strength；
  未签协议、履历/身份、意向或无实际行为的运行记录不能提升成熟度。

### 离线角色v2

- attempt、confirmation、deliberation均升v2；schema严格白名单并递归拒绝越权字段。
- statement、round response及嵌套文本禁止赋值成熟度或投资决定。
- evidence_refs必须来自六维视图的内容绑定许可，未知字符串拒绝。
- candidate digest覆盖完整候选；confirmation重新核对view、生产者、角色、证据许可、CaseBasis、
  dimension/criterion/claim/quote/subject/scope和findings。
- 人工decision保留`supports/does_not_support/rejected`，不再把`confirmed`硬编码为supports。

## 反例与验证

- 独立审核原始反例：10项全部复现错误接受/拒绝，原始日志SHA256
  `4812b005dad16214dea8680f9c9488f3bdc645fab1fe830dfeecad55cb53c3af`。
- 当前执行仓移植测试修复前：14 failed / 2 passed；修复后限定反例：16 passed。
- 新鲜夜间真实门控：180 passed / 0 failed / 0 skipped，exit 0。
- 新鲜最终全套：500 passed / 0 failed / 0 skipped，exit 0；唯一warning为既有zip重名拒绝测试。
- 批准wheel窄探针仍返回：`BRL_WEAK_COMMITMENT_FOR_LEVEL`、
  `IPRL_RIGHT_STATUS_NOT_VERIFIED`、`TMRL_EXECUTED_AGREEMENT_REQUIRED`。

## 真实副本结果

最终副本：`D:\t\kth-night-fix-final-cases-20260909\night-fix`。原`weijiu-r2b`未修改。

- manifest：`AGGMAN::2e90f9c4411f886c9f5d420adef4a6c7c169a3e5f6d0f3101bcae5c68b69ea82`。
- 六维视图：`OFFLINE6::16c6a732c5f7f5ae14d23ac73a7ba943ad04fa87adb251147909a8005c0113b5`。
- 新v2结果：BRL `2557c46a...04fcc`、TRL `dc82650b...f2c74`、IPRL
  `60d2c21a...e7322`、TMRL `4b5ee8b2...6812c`；FRL新结果`c0eb24fc...bb0b6`。
- 六维trace全部通过；0条非CRL review下，五个非CRL维度仍诚实为insufficient/0级。
- 角色工件schema为`kth-hybrid.offline-deliberation.v2`，实际证据许可1条；仍是离线模拟。

最终副本数据库SHA256：`357735471514db88d7ae1c27a297ca8e1a7e5b799fca126e02a8c50607c6ff69`。
最终快照SHA256：`3c7d34ef1f58b6e62fda213c746862eacf083f6327c90cf0a3b828b43d456674`。

## 保护与停止

原`weijiu-r2b`、R2-A.1、R2-A、R1.6及旧session hash均与开工前一致。未联网、未读凭证、
未调用真实模型/Provider，未merge/push/tag/release。整改包完成后停止，交fresh-context复验。

快照生成前两次因日志路径假设错误失败，失败JSON/log保留；run03成功，未隐藏失败。
