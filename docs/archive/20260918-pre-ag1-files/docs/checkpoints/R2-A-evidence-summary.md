# R2-A 证据与CRL结果摘要（脱敏）

> CRL离线分析核验记录，非正式投研报告、非D1、非完整六维结论。完整产物在
> `D:\t\kth-rebuild-cases\weijiu-r2a\audit\`。

## 最终分层存量

| 层 | 结果 |
|---|---|
| L0 | 捕获清单80、附件2、放弃传输2；80条均可读 |
| L1 | 非空正文40、零字节40；80/80 hash一致，依赖齐全 |
| L2 | 非空正文18种hash；来源独立性未完整审查 |
| L3 | Source85、Claim3、Qualification3；qualified/needs_review/rejected各1；82个Source实例未登记Claim |
| L4 | R1结果3；CRL规则13；真实met仅CRL1-C1，其余12条insufficient |

## CRL维度

- 结果：`CRLR2A::632aca0f...`；维度trace通过；SQLite历史记录1条。
- `attained_level=null`，`first_unmet_level=1`，维度产品状态`insufficient`。
- GOV只支持“政府页来源陈述市场需求假设”，不证明市场规模、产能、客户反馈或更高CRL条件。
- 规则覆盖：180条登记；CRL13已实现；其他167条未实现且未创建空模块。
- 运行历史23；CaseBasis版本1；CRL evidence review 1条。

旧导入期census保留历史，不覆盖。本摘要与`r2a-final-snapshot.json`均来自全部测试完成后的同一
数据库状态。
