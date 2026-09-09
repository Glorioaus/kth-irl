# NIGHT二次限定整改证据摘要

日期：2026-09-09。状态：执行方本地候选，独立复验待进行。

| 项 | 结果 |
|---|---|
| CRL正文trace | 冻结输入完整重算；正文伪造使trace与manifest失败 |
| view/license/角色 | 内容摘要重算、canonical权限、非空许可引用与confirmation闭包 |
| BRL | NaN/Infinity写入与内核双重拒绝；rule v3 |
| TRL9-C1 | 两种允许证据类均要求多用户与纵向期间；rule v3 |
| TMRL | 逐主体受控provenance overlay、runner冻结、trace重核；rule v3 |
| 二次反例 | 基线25 failed/1 passed；修复后26 passed |
| NIGHT真实门控 | 206 passed / 0 skipped |
| 最终全套 | 526 passed / 0 skipped；1条既有warning |
| 真实业务状态 | 五个非CRL review均为0，继续insufficient/0级 |
| 保护hash | 原Case与旧session六项全部不变 |

最终候选：`D:\t\kth-night-second-fix-final-cases-20260909\night-fix2`。

关键文件：

- `audit\copy-origin-proof.json`
- `audit\night-second-fix-artifacts.json`
- `audit\aggregation-manifest-v1.json`
- `audit\offline-six-dimension-view-v2.json`
- `audit\offline-role-deliberation-v2.json`
- `audit\night-second-fix-counterexamples-before-baseline.log`
- `audit\night-second-fix-counterexamples-final.log`
- `audit\night-second-fix-real-gate-final.log`
- `audit\full-suite-night-second-fix-final.log`
- `audit\protection-hashes-night-second-fix.json`

本摘要不是独立验收、正式评估、报告或投资决定。
