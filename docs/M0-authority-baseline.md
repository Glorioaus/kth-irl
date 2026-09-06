# M0 权威基线冻结文

日期：2026-09-06　|　机器可读钉定：`baseline/kth-hybrid-baseline.v1.json`　|　状态：M0 冻结

## 1. 权威层级与冲突规则（v1.1 §5，Owner 审核意见三）

1. **L0 Owner 明确业务要求最高**（2026-09-06 交接书 + 审核意见 + 最终覆盖指令）；
2. 未被 Owner 改变的业务行为，**以批准 wheel `2c490508…` 的实际行为为 canonical**；
3. KTH 三件材料（Compiled F PDF / User Guide / Dashboard XLSX）仅用于解释与发现差异，
   不构成对 wheel 行为的覆盖；
4. 文档与 wheel 行为冲突时，实现者与独立审核方均无裁定权，不得改变算法；
5. 有 KTH 方法专家时由专家裁定；无专家时维持 wheel 行为，分歧登记为
   future methodology review；
6. 旧 Case disposition（rev744/r158）仅为非权威校准对照，分歧必须规则级解释。

## 2. 钉定结果（全部 PASS，方法与数值见 baseline JSON）

| 钉定项 | 预期 | 实测 | 结论 |
|---|---|---|---|
| 执行 oracle wheel | sha256 `2c49050858555…471fe` | 逐字节一致（713,160 B；81 模块；70,709 行） | PASS |
| 源码解释基线（主仓 attached source，`src\kth_irl`） | 81 模块、80 一致、唯一差异 `v1/model_gateway.py` | 81 / 80 / 仅 `v1/model_gateway.py`；无单侧缺失 | PASS |
| `v1/model_gateway.py` | 已批准内部网关部署差异 | src `7bc611ea…` vs wheel `6c4a72fd…`；定性为部署差异，非业务差异，不参与判据提取与业务等价测试 | 已登记 |
| 旧 worktree 漂移源 | 58/81 一致、23 漂移 | 58 / 23（另有 11 个 staged 私有模块） | PASS（禁入） |
| 主仓钉定路径工作区状态 | 干净 | `plugins/kth-irl-evaluator-0.1.5`、runtime/data、runtime/sources、tests 均无未提交修改 | PASS |

主仓身份记录：HEAD `29159951ebe614aeed33567dfa314463f0e6ef80`（dev）；
attached source 最后提交 `8059ba27c1127389aa187930e0ff4243d2ff610d`（2026-08-10，
“功能：增加 KTH IRL 命令入口”）。

## 3. 导入边界

- **唯一源码导入源** = 主仓 attached source（80 一致模块；`model_gateway.py` 仅作部署
  差异参照）；**唯一执行 oracle** = 批准 wheel。
- 逐文件来源、hash、许可清单：`provenance/manifest.v1.json`（M0 已登记 wheel、源树、
  KTH 材料 3、Registry 7、原版测试 20、微玖附件 2 + 捕获 80；M1 导入时逐条核销）。
- 禁入清单（6 项）：旧 worktree 漂移源、旧 receipts/gates、kth-0.15-0828、rev744 状态、
  family-runs、旧 stage runtime。

## 4. 判据机械提取合同（M1 执行）

baseline-registry v1 的 criteria 为空（internal_shadow 模式），判据逻辑实际位于 wheel
代码内。M1 的 baseline.v2 显式化必须满足：

- 每条 criterion：`code_pointer`（80 模块基线内文件:行）+ `test_pointer`（原版测试）
  +（可机械定位时）`material_pointer`（KTH 材料）；
- `statement` 为代码判定语义的机械转写，审核方可逐条对照核验，不得含代码中不存在
  的规则；
- 无法机械对应即 `blocked`：停止该项、登记 spec-freeze-questions / divergence-ledger，
  呈 Owner，不得猜测；
- 判据数量下限（来自封存 Case disposition，非权威）：TRL ≥25、TMRL ≥38、IPRL ≥32、
  FRL ≥36，CRL/BRL 待提取。

## 5. 本基线的变更控制

- 任何字段变更 = 升版本（v2+）+ 差异审核记录；禁止就地修改。
- 复验触发：M1 开工时、每次审计导入前、每里程碑出口审核。
