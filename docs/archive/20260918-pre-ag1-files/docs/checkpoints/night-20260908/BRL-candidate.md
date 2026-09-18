# BRL 夜间离线候选

状态：独立验收未通过后已完成限定整改候选，等待fresh-context复验；不是独立完成。

## 资产差额

| 项 | 历史资产 | 新合同适配 | 新验证 |
|---|---|---|---|
| 36条与累计门 | 批准`brl_vertical.py`，SHA256 `d772ea82...a01bb` | 受控review生成准则处置，不复用旧session/Gate | 36条逐项正例/缺失、累计阻断 |
| 商业专业边界 | BRL specialist，SHA256 `aaea9854...e7e1` | CRL事实只能共享原件，不能共享BRL推理 | CRL等级/错证据类别拒绝 |
| 交易质量 | 原技能的预售、交付、销售和经营指标纪律 | 在准则finding中保留退款、价格桥、履约、客户数、期间和分母 | 预售、两客户销售、经营指标反例 |
| 评估单元 | 一个材料单元一次BRL | 有源单元字段与当前Case主体共同冻结 | 错单元、证明断链、完整trace |

原版直接测试参考`test_v1_pvs3_brl_vertical.py`，SHA256
`617a57a4...b9ce94`。本批不恢复旧主动研究或角色控制器。

## 候选能力

- 机械覆盖批准BRL D/2025的36条、四方面、1–9级累计门。
- 每条准则使用唯一finding及registry允许证据类别；缺失为原生/产品`insufficient`，
  明确反证为`not_met`，支持与反证冲突为`partial`。
- BRL6预售必须保留金额、可退性、目标价格桥、履约状态、买方身份和分母。
- BRL7商业模式要求商业条款下已交付给至少两个去重客户；BRL8/9经营指标要求期间和分母。
- Case入口复用通用维度review/result表、R2-A.1完整资格视图、内容寻址发布和统一trace。

## 真实门控

Case沿用本夜新目录`D:\t\kth-rebuild-cases\weijiu-r2b`，新增只在该Case中的
`case:assessment-units.json`。当前仅有公司级候选单元，明确不声称产品级拆分已经完成。

真实结果`DIMR2::BRL::dc78c10512ce64e755f70f3292346aee5a01bb9597cb872b4a285fc921130f21`：
36条全部`insufficient`、累计0级、首个未满足级1、trace通过、重放稳定。当前没有BRL专属
受控review，因此该结果只表示已审商业证据不足。

测试：规则`43 passed`；规则＋runner真实门控`47 passed`。FRL及完整回归在后续集成节点统一
重跑。未进入TRL/IPRL/TMRL、六维汇总、角色、正式决定或报告。
