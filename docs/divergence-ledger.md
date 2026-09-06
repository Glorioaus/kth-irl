# 业务分歧台账（divergence ledger）

开启：2026-09-06（M0）　|　规则：见《M0权威基线冻结文》§1——实现者与独立审核方无
业务裁定权；登记后由 Owner / KTH 专家裁定；无专家则维持 wheel 行为并标记
future methodology review。禁止删除条目；状态变更须留痕。

## 台账格式

`编号 | 描述 | wheel 行为指针 | 材料指针 | 发现人/日期 | 状态 | 裁定人与依据`

状态枚举：`open / owner-decided / expert-adjudicated / future-methodology-review /
closed-mechanical（机械定位后证实非分歧）`

## 条目

### DIV-001 baseline registry criteria 为空，判据逻辑在 wheel 代码内

- 描述：`data/v1/baseline-registry.json` 六维 `criteria: []`（internal_shadow，
  `not_evaluable_pending_source_mandate`），而判据判定逻辑实际位于 wheel 各 vertical
  模块代码内。
- wheel 行为指针：`v1/{crl,brl,trl,tmrl,iprl,frl}_vertical.py` 等（M1 逐判据定位）。
- 材料指针：KTH 三件材料（判据定义的解释参照）。
- 发现：M0 审计，2026-09-06。
- 状态：**closed-mechanical**——结构性事实，非方法分歧；处置 = M1 按机械提取合同
  显式化为 baseline.v2（不得补写）。

### DIV-002 `v1/model_gateway.py` 源树与 wheel 字节不一致

- 描述：src `7bc611ea…` vs wheel `6c4a72fd…`。
- 裁定：**owner-decided**（Owner 2026-09-06 审核意见二）——已批准的内部网关部署
  差异，非业务差异；不参与判据提取与业务等价测试。

### DIV-003 旧 worktree attached source 相对批准 wheel 23 模块漂移

- 描述：58/81 一致；漂移清单见《M0源码漂移审计》§3。
- 裁定：**owner-decided**（Owner 2026-09-06）——漂移失败案例，禁止导入，仅作反漂移
  回归对照。

### DIV-004 整链测试以失败路径为主，完整 happy-path fixture 需组装

- 描述：attached source `tests/test_v1_local_whole_chain.py`（149 测试）主体为
  fail-closed/resume 路径；完成路径以
  `test_provider_backed_fixture_completes_product_scopes_and_one_shared_frl` 等
  provider-backed fixture 形式存在。
- 处置：非业务分歧；登记为 M0.5 链 A 的输入事实（见 spec-freeze-questions Q11）。
- 状态：**closed-mechanical**（观察登记）。

## 待办观察位（M2 起预计新增）

- 新系统与旧 Case disposition 的逐判据分歧（M2）；
- 文档（KTH 材料）与 wheel 行为的语义冲突（随时登记，均不得由模型裁定）。
