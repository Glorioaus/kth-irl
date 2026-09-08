# R2-A.1 证据与结果摘要（脱敏）

> 仅记录完整资格视图限定修复；不是独立验收结论、正式 D1、报告或完整六维评估。

## 修复结果

| 项 | 结果 |
|---|---|
| 时间版本 | 冻结实际修订号、创建时间和完整快照；trace 按绑定修订重建 |
| 资格结果 | 冻结完整 QualificationOutcome、允许用途、限制、状态和方法版本 |
| 证明关系 | 冻结 CaseBasis 主体/别名、第一方归属、时区及登记时间实际绑定 |
| 输入身份 | 完整资格视图进入维度摘要；不同时间依据生成不同 result_id |
| 发布保护 | 发布前与读时 trace 共用同一 payload 校验 |
| 历史语义 | v3 旧修订在仅追加新修订后仍可重核；删除/修改旧绑定会失败 |

## 真实结果

- Case：`D:\t\kth-rebuild-cases\weijiu-r2a-1`。
- v3 结果：`CRLR2A::364e41be3c22a70cc7ff236697b734284d4ded0e2768719d0f0b14ac56c478f5`。
- 资格视图摘要：`a8f36650ab78c279fbf179ae6554d0179a5cc6846af1a6df56eb0a4a14b73027`。
- 实际时间修订 1，实际证明键 `case_basis`、`timezone`，trace 通过，重放稳定。
- `CRL1-C1=met`；其他 12 条 `insufficient`；未形成累计等级，首个未满足级为 1。
- 主体仍为 claimed，未工商核验；未扩写政府页支持范围，未强制微玖达到任何等级。

## 验证索引

- 专项真实门控：`real-gate-r2a1.log`，35 passed。
- R1.6 真实门控：`real-gate-r1-r2a1.log`，3 passed。
- 全套：`full-suite-r2a1.log`，264 passed、0 skipped。
- runner/trace：`real-runner-trace-r2a1.log`。
- 三类隔离反例：`r2a1-counterexample-results.json`。
- 最终快照：`r2a1-final-snapshot.json`。

上述完整文件均位于 `D:\t\kth-rebuild-cases\weijiu-r2a-1\audit\`，hash 与原始命令见
`R2-A.1-handoff.json`。R2-A 原交接和 Case 保持只读历史。
