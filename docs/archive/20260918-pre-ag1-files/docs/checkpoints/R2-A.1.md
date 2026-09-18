# R2-A.1 检查点（2026-09-08）

范围：仅修复 R2-A 维度结果的完整资格视图冻结、发布前核验与读时 trace。
保留 R1 已验收入口、13 条 CRL 规则、事务、内容寻址和不可变结果历史；未进入其他维度、
R2 后续、真实模型/Provider、D1/D2、报告或发布。本文件为执行方交接，不声称独立审核通过。

## 基线与提交

- 起始分支/HEAD：`codex/runtime-rebuild` / `194a8f3e7ebc18b5afbc5e6ff93929deb95bf54e`。
- 实现提交：`465de9b81c2d08a63555cc33d5863bf03a5907e7`，
  `修复(R2-A.1)：冻结并追溯完整资格视图`。
- 实际导入：`D:\UGit\kth-irl-hybrid\plugin\src\kth_hybrid\runner.py`、
  `audit.py`、`qualification.py`；专项日志同时记录了运行时 `__file__`，未加载旧审核快照。
- 新 Case：`D:\t\kth-rebuild-cases\weijiu-r2a-1`。由只读 R2-A Case 复制，复制前后
  `records.sqlite3` SHA256 均为
  `82470ac923d03c1aec22bac3bff2ea1a5715f8297d40df2f6a21d6931fe3b094`。

## 限定实现

每个实际进入 CRL 规则的 review 现在绑定 `qualification_view`：

- 完整 Claim、Source、存储资格记录及各自摘要。
- 本次选中的时间证据修订号、创建时间与完整内容快照；历史 trace 按该修订重建，不读 latest。
- 本次重新计算的完整 `QualificationOutcome`，含四类判断、允许用途、限制、状态、
  `kth-hybrid.qualification.v4` 方法版本和复核尝试标识。
- R1 同一路径解析器产生的 CaseBasis 主体/有效别名、第一方归属、时区及登记时间证明绑定。
- 单个资格视图摘要进入维度冻结输入；维度结果 schema 升为
  `kth-hybrid.r2a-crl-dimension.v3`。

`validate_crl_dimension_payload` 同时由发布前和 `trace_crl_dimension` 调用。若原本可发布业务
结果但发布前绑定核验失败，所有原生处置清空并转为 `execution_failed`；已经识别的执行失败
仍可作为失败历史落库，不转业务 NO 或普通证据不足。

## 反例与配对

当前执行仓新增 `plugin/tests/test_r2a1_qualification_view.py`。核心四项在修复前实测
`4 failed`：删除时区证明和时间修订后 trace 仍通过、换用新时间依据仍复用旧身份、第一方
别名结果没有 `qualification_view`。修复后专项覆盖 8 个测试实例，包含：

- 删除或修改已绑定时间修订，旧 trace 失败。
- 删除已绑定时区证明，旧 trace 失败。
- 追加 `+07:00` 新规则/修订后，新运行获得不同输入与结果身份；旧、新 trace 均通过，
  同一新输入重放身份稳定。
- 规范名和合法别名第一方均冻结实际 document ownership；删除归属或别名证明时 trace 失败。
- 发布前统一校验故障不能暴露 `met`。

隔离真实探针结果见
`D:\t\kth-rebuild-cases\weijiu-r2a-1\audit\r2a1-counterexample-results.json`：

- 删除时区证明导入后，旧结果 trace 报时区绑定及 QualificationOutcome 变化。
- 删除时间修订 1 后，旧结果 trace 报该修订已删除或不可读。
- 新增时间修订 4 后，结果身份由 `364e41be...` 变为 `6c3ed4fb...`；仅追加新修订时
  原 `364e41be...` trace 仍通过，新结果 trace 也通过。

## 测试与真实结果

- 资格、R1.5/R1.6 证明、CRL 规则及 R2-A 入口关联回归：`64 passed, 1 skipped`；该局部
  skip 仅因当时未设置真实 Case，最终验证不依赖 skip。
- R2-A/R2-A.1 专项及真实门控：`35 passed, 0 skipped`，exit 0。
- R1.6 真实三切片：`3 passed, 0 skipped`，exit 0；PyPDF2 既有弃用 warning 1 条。
- 最终全套：`264 passed, 0 failed, 0 skipped`，exit 0；唯一 warning 为 zip 重名拒绝测试。

真实 v3 结果：

- `result_id/input_digest`：
  `CRLR2A::364e41be3c22a70cc7ff236697b734284d4ded0e2768719d0f0b14ac56c478f5`。
- 完整资格视图摘要：
  `a8f36650ab78c279fbf179ae6554d0179a5cc6846af1a6df56eb0a4a14b73027`。
- 实际时间修订 1；实际证明键为 `case_basis`、`timezone`；完整 trace 通过且重放稳定。
- 仍仅 `CRL1-C1=met`，其余 12 条 `insufficient`；`attained_level=null`，
  `first_unmet_level=1`。未为形成等级补造材料或改变主体/截止。

旧 v2 结果仍在 SQLite 不可变历史中，但它本身没有 R2-A.1 所要求的完整资格视图，因此新
trace 明确报不完整；这不是覆盖旧结果。版本配对承诺由 v3 结果验证：追加新时间修订不破坏
其绑定的旧修订，删除或修改该旧修订/证明则可见失败。

## 最终快照与边界

最终快照：`D:\t\kth-rebuild-cases\weijiu-r2a-1\audit\r2a1-final-snapshot.json`，
SHA256 `a8ec2e1942af8c4006bb6be016bc9c6e35d2bc6554929c1866e8cd548e7f74be`。
最终数据库 SHA256 为
`493deb41afabd1b6053e64f042c7afb600578770f364ce09bbe5b04146d1a302`；Sources 85、Claims 3、
Qualifications 3、R1 结果 3、CRL reviews 1、维度历史 2、runs 33。覆盖清单仍为 180 条，
CRL 13 条已实现，其他 167 条后续未实现。

旧 R2-A、R1.6 数据库和旧 session 的保护 hash 与交接前一致。全程零网络、零真实
Provider/运行时模型 API，未读凭证、未改 ACL/注册表、未运行旧修复脚本，未 merge/push/
tag/release。R2-A.1 完成后停止送审，不自动进入其他阶段。
