# M0 微玖 Golden Case 资产清单（冻结）

日期：2026-09-06　|　机器可读：`baseline/kth-hybrid-baseline.v1.json -> golden_case`、
`provenance/manifest.v1.json -> entries.golden_case_*`　|　结论：**资产完整，无缺失、
无损坏，贯穿式 Golden Case 路线（M0→M5）具备输入条件**。

## 1. Case 身份

- case_id：`LOCAL-CASE-e7edcba32c19969a317f4bca`
- 封存 session：`D:\t\kth-phase8-real-session-e7edcba32c19969a317f4bca\session`（只读）
- 旧状态：revision=744，phase=dimension_set，
  session_hash=`a93e446f63b6ed7be004900af93f5fddd86197b6131b63e36b5d2d80dd675965`，
  formal_admission=false（旧链不作数，仅作对照）

## 2. 附件（2 件，哈希与 attachment-inventory.json 记录逐字节一致）

| 存储名 | 原名 | 字节 | sha256 |
|---|---|---|---|
| inputs/attachments/0000.bin | 微玖光电BP260716.pdf | 3,355,758 | `d2d8a79d6b5202e6b15f7db44c5b14db6125ee025f880e510beab6d2df4284e8` |
| inputs/attachments/0001.bin | Pre-A高管及客户访谈.zip | 79,326 | `12d7ab408f01ed81d484559a94b3557d216e18b7dd60fc7b63d30778d85034a7` |

## 3. RawCapture 资产池

- `research\` 共 490 文件、83 个目录，其中：
  - **80 个完整捕获目录**（`FAMILY-PRIVATE-RESEARCH-ACTION-*`）：每个均含
    `frozen-capture/raw-body.bin` + `frozen-capture/transport.json` + `receipt.json`，
    **80 个 raw-body 已全部完成 sha256 钉定**（逐条见 provenance manifest）；
  - 2 个**显式 abandonment 失败传输目录**（`…e155ecd…`、`RESEARCH-ACTION-17edc3…`）：
    仅含 abandonment/request/transport-attempt/transport-failure，无捕获——定性为诚实
    失败证据，非资产缺失（对应 Q9）；
  - 1 个 `inventory-history` 簿记目录（旧 gate 世代，非证据资产）。
- `research-qualification-revisions\`：183 份 qualification 工件（重资格化对照输入，
  非权威；多对多关系见 Q10）。

## 4. 旧业务 disposition 对照基线（非权威校准件）

| 维度 | 旧 disposition |
|---|---|
| 研究任务 / 专利任务 | 12/12、9/9 已封存 |
| CRL | 2 项 met，其余未满足 |
| BRL | 证据不足为主 |
| TRL | 25 项 not_met |
| TMRL | 38 项 not_met |
| IPRL | 5 项 met、27 项 not_met |
| FRL | 36 项 not_met |
| callback 实数 | crl_brl 17 / trl_tmrl 17 / iprl 8 / frl 10 |

用途：M2 起逐判据分歧台账对照；**禁止**作为新正式链权威输入。

## 5. 复用边界（贯穿式路线）

| 里程碑 | 微玖任务 |
|---|---|
| M0（本文件） | 资产清单冻结 + disposition 对照登记 |
| M1 | 导入 1 附件 + 1 RawCapture + 1 Evidence（管线冒烟） |
| M2 | 全量 80 捕获重验证（哈希比对）→ 重资格化 → 六维全跑 + 逐判据分歧台账 |
| M3 | 微玖执行 D1（需 org-d1-policy 已冻结） |
| M4 | 微玖首份完整报告（三标签渲染） |
| M5 | 分歧清理 + Golden Case 专项审核 |

禁止复用：rev744 session 状态、Decision B dimension-set、r158 formal hash、publication
authority、跨世代 Gate、旧 callback/session receipt 身份。原则上不重新联网；任何来源
缺失或无法验证 → 停止并呈 Owner。
