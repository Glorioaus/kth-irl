# M0 源码漂移审计

日期：2026-09-06　|　方法：wheel 解包后与两处 attached source 逐模块 sha256 字节比对
（脚本与结果存于仓库外 `D:\t\m0-scratch-20260906\`，不入库）　|　结论：**四套源码身份
确认，权威基线与禁入边界已钉死**。

## 1. 存在的四套身份

| 身份 | 位置 | 与批准 wheel 关系 | 处置 |
|---|---|---|---|
| 批准 wheel | `sunny-skills\plugins\kth-irl-evaluator\runtime\…0.1.5…whl` | 本体（sha256 `2c490508…471fe`） | 唯一执行 oracle |
| 主仓 attached source | `plugins\kth-irl-evaluator-0.1.5\…\src\kth_irl` | 81 模块，**80 逐字节一致** | 唯一源码导入源 |
| 旧 rev744 worktree 源 | `.codex-temp\agent-driven-staged-product-completion\plugins\kth-irl-evaluator-0.1.5\…\src\kth_irl` | 92 模块，**58 一致、23 漂移 + 11 私有** | 禁入；反漂移对照 |
| kth-0.15-0828 快照 | `sunny-skills\kth-0.15-0828\`（未跟踪） | 另一 wheel（`920c36f7…`）+ 94 模块源（多 13 模块） | 完全隔离 |

## 2. 唯一批准差异：`v1/model_gateway.py`

- src sha256 `7bc611ea1c2613baa6d5221997105d43450409493179f8f6fbb67c94070a5142`
  vs wheel sha256 `6c4a72fd14e9a6aac287e6b308346fd6fe5e686bc79899b2969524e331817446`；
- 定性（Owner 2026-09-06 审核意见二）：**已批准的内部网关部署差异，非业务差异**；
- 纪律：判据提取、业务等价测试不使用该模块语义；M1 审计导入时该文件按部署差异标注。

## 3. 旧 worktree 漂移清单（23 模块，禁止导入）

```
__init__.py                      delivery.py                      v1/__init__.py
v1/acquisition.py                v1/active_crl_brl_runtime.py     v1/active_evidence.py
v1/active_iprl_runtime.py        v1/active_trl_tmrl_runtime.py    v1/capsule.py
v1/crl_community.py              v1/dimension_integration.py      v1/dimension_ports.py
v1/iprl_competition.py           v1/iprl_patent_discovery.py      v1/iprl_predispatch.py
v1/local_whole_chain.py          v1/model_gateway.py              v1/patent_adapters.py
v1/provider_host.py              v1/research_dispatch.py          v1/research_frames.py
v1/subject_identity.py           v1/tmrl_professional_runtime.py
```

另有 11 个旧 staged 私有模块（wheel 与主仓源均不存在），同为禁入：

```
v1/active_assessment_scope.py        v1/active_d2_source.py
v1/active_dimension_set.py           v1/active_product_case_orchestration.py
v1/active_publication_authority.py   v1/active_semantic_control_authority.py
v1/active_shared_snapshot.py         v1/active_unit_dimension_source.py
v1/formal_dimension_report_source.py v1/staged_decision_input.py
v1/unit_scoped_professional_authority.py
```

## 4. 反漂移回归用途

M1 起的源码一致性测试必须以旧 worktree 漂移树为负样本：任何“attached source 与 wheel
一致性”检查若不能把这 23 个漂移模块全部判为差异，即测试无效。

## 5. 审计结论

主仓 attached source 与批准 wheel 的关系和 Owner 独立复核完全一致（80/81、唯一差异
model_gateway.py；旧 worktree 58/81、23 漂移）。授权漂移问题就此关闭：M1 之后唯一合法
的源码变化路径是新仓库自身的受审提交，外部源码身份一律以本文件与 baseline JSON 为准。
