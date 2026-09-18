# NIGHT局部闭环修复检查点

日期：2026-09-09。状态：执行方候选完成，等待新的fresh-context独立复验；不声称独立通过。

## 授权与提交

- 授权输入：`D:\t\kth-night-fix2-astra-final-review-20260909\给Owner与执行层的NIGHT局部闭环修复输入.md`。
- 起始HEAD：`ba9f47d2a2bac1b34a0d8c4c8af8bac77c2daa0f`。
- 测试提交：`d8d95bd`；最后全绿代码提交：`7400af1`。
- 仅修改`kernels/trl.py`、`audit.py`、`roles.py`及对应测试。

## 三项修复

1. TRL9对用户ID先`strip()`再去重，两种允许证据类统一执行；TRL规则升为
   `kth-hybrid.trl.night.v4`，不外溢TRL8。
2. TMRL trace核对overlay的CaseBasis、scope和subject关系，从live record中的三条ref重新解析，
   验证同记录关系，并把重建record/proof与冻结overlay整体比较。
3. 角色递归扫描新增决定字段文本赋值语法门，覆盖冒号、等号、空白、大小写、JSON字符串及
   PRO/CON/CHAIR、round、confirmation和嵌套字符串；中性字段名提及继续允许。

P2的support_scope/criterion/allowed_uses和命名aggregation profile未处理，继续单列。

## 验证

- `d8d95bd`独立基线路径实际导入旧实现：`24 failed / 11 passed`。
- 修复后局部反例：`35 passed`。
- 审核方原脚本：角色`12 passed`、TRL持久化链`6 passed`；两份TMRL伪造Case均被trace拒绝。
- NIGHT 11模块：`241 passed / 0 skipped`。
- 最终全套：`561 passed / 0 skipped`，仅既有ZIP重名warning。

## 新候选

Case：`D:\t\kth-night-local-closure-final-cases-20260909\night-fix3`。

- 从`night-fix2`复制1,874个文件，复制前树hash均为
  `5df62a42d42f44f8b9e9d4aa7091f8fb73725901e0fd92e2cb9031410932002e`。
- 数据库SHA256：`7db82213a9f3f27880a7215d056a201c0f55cd734fd0281e55f692ea991a95a4`。
- 新TRL v4结果：`DIMR2::TRL::e0891c3e3adc3176d297c90ea9bdb16827c646ce26d43b454b22ce02310155a9`。
- manifest：`AGGMAN::3f15f459093d2cadfe5e3a9b0967e2a88b252c5f784eb81768bf0b8015dbeb56`。
- view：`OFFLINE6::cb3c545099acf81feb5f7ddefe7b07280ca1fb5d6cef7807995f398cfa75b602`。
- 其余五维结果ID保持；六维trace和manifest/view/role重建均通过。
- 真实非CRL review和TMRL overlay仍为0，五维保持insufficient/0级。

## 保护与停止

原weijiu-r2b、两轮night候选、R2-A.1、R2-A、R1.6及旧session hash均不变。
未联网、未调用真实模型/Provider，未进入D1/D2、报告、投资决定、发布或新Gate。本批到此停止。
