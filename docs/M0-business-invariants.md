# M0 业务不变量冻结文

日期：2026-09-06　|　状态：M0 冻结　|　用途：全部条目必须落为可执行测试（T0 属性测试 +
各层覆盖），违反任一条即为发布阻断。

来源：Owner 交接书 §七（INV-01…14）+ v1.1 审核意见（INV-15…20）。

## 第一组：评估语义（INV-01…09）

- **INV-01 六维定义不变**：CRL、BRL、TRL、TMRL、IPRL、FRL 六维及其业务定义不得增删
  或重命名；判据语义只能来自机械提取（见权威基线 §4）。
- **INV-02 Registry 权威**：判据与方法资料的唯一数据源是经审核的 Registry v2；
  未经差异审核不得修改。
- **INV-03 证据真实性**：搜索结果在 RawCapture 使命取回之前不是 Evidence；每条 Evidence
  强制绑定 capture_id；RawCapture 内容只写一次、不可变；CaptureReceipt 每捕获一份。
- **INV-04 三重资格**：Evidence 必须通过来源资格（source-policy 七类）、时间有效性
  （参考时间窗）、身份有效性（主体匹配）三重资格化，任一不通过不得支撑 met。
- **INV-05 五态语义**：`met / partial / not_met / not_applicable / insufficient` 的业务
  含义以 wheel 行为为 canonical；partial 在 maturity gate 中视同未满足（待 M1 差分
  复核，见 spec-freeze-questions Q6）。
- **INV-06 严格逐级 maturity**：第 N 级要求 ≤N 级全部判据 met；不得跳级、不得聚合
  分数替代。
- **INV-07 无证据不 met**：无合格 Evidence 的判据不得 met（只允许其余四态）。
- **INV-08 零结果不推断不存在**：零结果检索记录为“已检索、零结果”，绝不得推断事实
  不存在。
- **INV-09 六维不可互替**：任何维度的结果不得由其他维度的分数或结论替代；D1 是唯一
  合法汇聚点。

## 第二组：决策与报告（INV-10…14）

- **INV-10 D1/D2 原则**：D1 fail-closed、仅 YES/NO；D2 语义以 M1 机械定位结果冻结
  （spec-freeze-questions Q1）。
- **INV-11 报告必含**：事实、证据缺口、少数意见、翻转条件四要素；缺失即报告无效。
- **INV-12 不足仍完成**：Evidence 不足时评估与报告必须正常完成且保守输出，如实呈现
  insufficient，不得因不足而中断或编造。
- **INV-13 仅 YES/NO**：正式决定只允许 YES 或 NO（含两种 NO 标签，见 INV-15）。
- **INV-14 YES 边界**：本案 YES 仅代表批准进入正式投资尽调；不批准金额、估值、付款、
  签约或交易条款。

## 第三组：结论与执行完整性（INV-15…20，v1.1 新增）

- **INV-15 结论三分**：业务结论仅可由完整、通过 provenance 与结构校验的
  PolicyChoice/Advocate/Chair 工件产生；结果枚举
  `YES | NO(formal) | NO(insufficient_evidence) | DECISION_UNAVAILABLE`；
  模型超时/畸形/非法引用/上下文泄漏/两次校验失败 ⇒ execution_failed，**永不转 NO**，
  不得向用户暴露伪业务结论；报告必须区分三种状态。
- **INV-16 恰好一次**：每个真实 Provider action 恰好一次；mission 状态先落盘再行动
  （write-ahead）；恢复只读 sealed capture；outcome_unknown 禁止自动重试（唯一例外：
  已验证 idempotency key 的同键恢复）；失败证据永久保留。
- **INV-17 判据不可发明**：模型不得补写或改写 criterion；无机械对应即停。
- **INV-18 canonical 纪律**：wheel 行为 canonical；模型不得因文档冲突改算法；业务分歧
  人类裁定（Owner/KTH 专家），否则登记 future methodology review 并维持 wheel 行为。
- **INV-19 凭据纪律**：不主动读取、不显示凭据具体值；凭据不出现在日志、聊天、报告、
  receipt；凭证治理为非阻断 backlog（Owner 2026-09-06）。
- **INV-20 禁入纪律**：禁止从 forbidden_sources（旧漂移源、kth-0.15-0828、rev744 状态、
  family-runs、旧 stage runtime）导入任何业务代码或状态。

## 落测映射

| 不变量 | 测试层 |
|---|---|
| INV-03/04/07/08 | T0 属性测试 + T1 coverage C1–C6 |
| INV-05/06/09 | T0 + T1（maturity 跨级阻断 C7） |
| INV-10/13/14/15 | T0 决策类 + T7 链验证器对抗样本 |
| INV-11/12 | T4 Golden Case + T6 报告渲染三标签 |
| INV-16 | T3 崩溃注入（claim 前/后、dispatch 前/后、seal 前） |
| INV-17/18/20 | 流程审核 + provenance CI 检查 |
| INV-19 | T5 扫描（凭据值零出现） |
