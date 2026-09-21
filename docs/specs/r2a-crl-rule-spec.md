# R2-A CRL 离线规则规格

范围：批准 wheel registry 的 CRL1-C1 至 CRL4-C4，共13条；仅1–4级。规则原文、级别及
code pointer 机械来自 `r2-rule-coverage.json`。CRL5–9未实现，不能生成等级或结论。

每条规则只消费已落库的 `crl_evidence_reviews`：记录绑定 CaseBasis版本、Claim、准则、
封存摘录hash、decision、findings、scope、reviewer和review_basis。没有可用复核为产品
`insufficient`、原生处置null；受控反证为原生`not_met`；冲突支持/反证为`partial`；满足
对应最小findings为`met`。这不是R1的`succeeded`改名。

规则最小findings分别为：

- CRL1-C1 市场需求假设；C2 客户/问题假设可识别；C3 初步市场与潜在客户知识。
- CRL2-C1 二手市场研究；C2 市场、客户、问题/需求与替代方案熟悉；C3 清晰问题/需求描述。
- CRL3-C1 至少一名直接用户/客户/专家反馈联系人；C2 客户分群；C3 反馈后更新假设。
- CRL4-C1 去重后至少两名客户/用户联系人且重要性确认；C2 初步客户画像；C3 用户、付费客户、决策者角色；C4 有客户反馈的替代方案定位。

累计规则：每一等级全部 applicable 准则原生`met`才进入该级；第一个未满足等级阻断更高级。
不按met数量平均，不因高等级结果绕过低等级缺口。真实R2-A材料只支持CRL1-C1，维度结果
保持未形成累计等级、首个未满足级为1。

方法差异：批准 wheel 的CRL编译器接收宿主adjudication；本批实现受控离线findings合同，
未声明与宿主adjudication全等价。原版测试未能机械定位到单条criterion的，覆盖清单标记
`未机械定位`，不虚构test pointer。
