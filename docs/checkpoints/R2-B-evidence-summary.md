# R2-B FRL 证据与结果摘要

状态：执行方本地自检候选，独立审核待进行。

| 项 | 结果 |
|---|---|
| Registry | FRL 36条，1–9级，批准wheel F/2025 internal shadow |
| N/A | 27条never，9条仅限有源“不计划外部融资”政策 |
| 融资主体 | `FIN-WEIJU-COMPANY`，当前Case公司级单元共享一次结果 |
| 真实review | 0条FRL专属受控review |
| 真实结果 | 36条insufficient，累计0级，首个未满足级1 |
| Trace | 有效结果完整trace通过，同输入重放稳定 |
| 覆盖清单 | CRL 13已验收；FRL 36候选实现；其余131未实现 |

测试：FRL真实门控`56 passed`；最终集成全套`320 passed / 0 skipped`。原版FRL窄对照
`6 passed / 1 WinError5`，保留为旧目录发布I/O差异，不移植该发布方式。

原始日志与最终快照位于`D:\t\kth-rebuild-cases\weijiu-r2b\audit\`；完整命令、退出码和
SHA256见`R2-B-handoff.json`。
