# 六维离线候选汇总

状态：执行方本地自检候选，独立审核待进行；不是正式评估、报告或投资决定。

`aggregate.build_offline_dimension_view`只读取同一CaseBasis版本和主体scope下实际存在且trace通过的
CRL、BRL、TRL、IPRL、TMRL、FRL结果；缺维、错scope、错版本、重复维度或断链均拒绝汇总。
不计算总分，不把`execution_failed/method_unsupported/insufficient`转成业务NO。

真实视图`OFFLINE6::7b98bf147d6cf8d6c6b69df7befc6cacc9cfbb7774b0c9d7105254f456c703d7`，
文件位于`D:\t\kth-rebuild-cases\weijiu-r2b\audit\offline-six-dimension-view.json`。
六维trace均通过；CRL仅CRL1-C1为met且未形成累计级，其余五维当前均0级/证据不足。
该结果反映当前已审覆盖，不声称全部材料已提取或企业实际成熟度为零。
