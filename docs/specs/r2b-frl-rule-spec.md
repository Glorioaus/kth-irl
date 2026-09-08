# R2-B FRL 离线候选规则规格

日期：2026-09-09。状态：执行方本地自检候选，独立审核待进行。

范围为批准 wheel `kth_irl.v1.frl_vertical.get_frl_registry()` 的 36 条详细准则、
1–9级累计门、融资主体共享与受限 N/A。不是官方 KTH 评估、估值、投资决定或法律/会计意见。

## 历史资产差额

| 功能 | 可信历史资产 | 新合同差异 | 本批处理 | 新验证 |
|---|---|---|---|---|
| 36条registry与累计门 | `frl_vertical.py`，SHA256 `96097ff0...54ada` | 旧编译器消费已给定mapping，不核新资格视图 | 保留证据类别、N/A及累计语义；以受控review生成候选处置 | 36条逐准则正例/缺失反例、累计阻断 |
| 资金类别 | wheel `commitment_evidence_rule`及FRL specialist | 旧技能依赖旧专业运行与外部研究 | 本批只实现离线类别门，不恢复旧session/Gate | 软意向、条件承诺、可用现金、预收义务配对 |
| 融资主体 | FRL specialist，SHA256 `fe8f9c91...c9ea` | 新Case需有源scope并供多个单元共享 | 一个融资主体结果冻结全部当前单元引用 | 错主体、重复单元、证明断链 |
| 发布与trace | R2-A.1完整资格视图 | 旧FRL无新线不可变结果表 | 复用`build/verify_qualification_input_view`，新增通用维度review/result表 | 原件/Claim/时间断链、重放、发布失败 |

原版直接测试参考 `test_v1_pvs6_frl_vertical.py`，SHA256
`e746a639...cad13`。本批比较业务语义，不要求旧目录或receipt格式相同。

## 规则输入与结果

每个正向 review 必须绑定：CaseBasis版本、Claim、准则、原文摘录hash、融资主体、证据类别、
准则专属finding、支持范围、复核人和复核依据。runner对实际消费的Claim建立R2-A.1完整资格
视图；资格通过不自动等于准则met。

处置：

- 合格支持review满足准则专属finding且证据类别在registry允许集合中：`met`。
- 合格明确反对review：`not_met`。
- 同时存在支持与反对：`partial`，不按数量表决。
- 无足够合格review：原生及产品均为`insufficient`，不推定低级负面状态或资金事实成立。
- 完整性/执行故障：清空全部原生处置，维度`execution_failed`。

36个finding字段逐条唯一，位于`kernels/frl.py::RULE_REQUIREMENTS`；不能用同一全true字段替代。

## 资金确定性

资金桶固定为：`cash_now`、`closed_or_drawable`、`binding_conditional`、
`soft_or_nonbinding`。FRL3/4/8的可用资金或runway只接受前两类；FRL5可接受条件承诺或已关闭/
可提用资金；FRL6/7/9按registry允许讨论、term sheet或具体兴趣，但不提升为到账资金。

预收款若作为现金输入，必须同时保留交付和退款义务；义务被隐藏时不能支持正向处置。

## 受限 N/A 与累计

机械核对结果为27条`never`、9条`explicit_no_external_financing_only`。仅当同一封存逻辑记录
同时绑定当前融资主体、Case主体和严格布尔`external_financing_planned=false`时，9条外部融资
相关准则可为`not_applicable`。缺证明、跨主体、跨记录拼接或字段变化均不放行。

累计与批准wheel一致：从1到9，每一级检查所有不高于该级的准则，只有`met`或合法
`not_applicable`才通过；首个缺口阻断更高级。没有任何已满足级时为0，不计算平均分或跨维总分。

## 当前真实边界

`weijiu-r2b`只冻结当前Case唯一公司级融资主体
`FIN-WEIJU-COMPANY`及当前公司级单元引用。现有三条已资格/待审主张没有FRL专属受控review，
因此真实候选结果36条均为`insufficient`、累计0级。这表示当前已审材料不足，不表示企业没有
融资、没有现金或不值得投资。产品级评估单元、资金台账、到账/授信、融资政策和完整FRL专业
分析仍未形成。
