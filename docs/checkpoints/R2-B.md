# R2-B FRL 离线候选检查点

日期：2026-09-09。状态：执行方本地自检通过，独立审核待进行。

## 能力与边界

- 机械消费批准wheel的FRL 36条、1–9级、27条never及9条受限N/A。
- 每条规则有唯一finding、允许证据类别、缺失/反证/冲突行为；不以统一全true或关键词代替。
- 区分`cash_now`、`closed_or_drawable`、`binding_conditional`、`soft_or_nonbinding`；
  软意向和条件承诺不能支持到账或可用现金准则，预收款保留交付/退款义务。
- FRL按一个有源融资主体运行一次，冻结当前受影响评估单元引用，不产生产品单元独立FRL等级。
- runner复用R2-A.1完整资格视图；通用维度review/result进入SQLite事务与内容寻址历史，
  发布前和trace使用同一完整绑定校验。
- 未进入BRL/TRL/IPRL/TMRL、六维汇总、角色合同、D1/D2、报告或发布。

实现提交：`bf7db2cfd5d64d4ebccc00b1ce964b1299c6a4dd`。

## 历史资产复用

批准`frl_vertical.py`提供36条registry、证据类别、受限N/A和累计门；FRL specialist提供
融资主体、资金确定性、产品单元共享和禁止投资决定的边界。本批复用这些业务语义，替换旧
session/Gate及会触发WinError5的目录整体替换发布。

原版FRL纵向测试窄对照为`6 passed / 1 failed`；失败位于原版`os.replace(staging,target)`的
已知Windows拒绝访问，不属于新规则语义失败，也未通过修改旧源码规避。

## 真实候选

Case：`D:\t\kth-rebuild-cases\weijiu-r2b`。由R2-A.1只读Case复制，初始数据库SHA256
`493deb41afabd1b6053e64f042c7afb600578770f364ce09bbe5b04146d1a302`。

只在新Case封存融资主体范围：`FIN-WEIJU-COMPANY`，当前单元
`UNIT-WEIJU-COMPANY-CURRENT-CASE`。产品级单元尚未冻结，不冒充已完成多产品评估。

有效结果：

- `DIMR2::FRL::16f3409dc98d9b85ec8e533de4c92f4bc10ef8b2977b4f6a3e70b58242f558cc`
- 36条均为原生/产品`insufficient`，累计0级，首个未满足级1。
- 当前没有FRL专属合格review，完整资格绑定数量0；融资主体证明及结果trace通过。
- 同输入重放身份稳定。此前错评估单元门控形成的执行失败历史保留，维度历史共2条。

这只表示当前已审材料没有形成FRL专属受控证据，不表示企业没有资金、没有融资活动或不适合投资。

## 验证

- FRL规则：`48 passed`。
- FRL规则＋Case入口＋真实门控：`56 passed`，exit 0。
- FRL/CRL/资格/store/audit关联回归：`107 passed, 2 skipped`；局部skip仅因未设置真实变量。
- 最终集成全套：`320 passed, 0 failed, 0 skipped`，exit 0；唯一warning为既有zip重名拒绝测试。

最终快照：`D:\t\kth-rebuild-cases\weijiu-r2b\audit\r2b-frl-final-snapshot.json`，SHA256
`5dc8318b2ca5137c564f02fee57ae146917f6d535f4c8d91bd78fb128efc395d`。最终数据库SHA256
`303059a5b6edb6d68fe77bae0ede39dd2b13fe496753a815ac06dbe594ac338b`。

旧R2-A.1、R2-A、R1.6数据库及旧session保护hash未变化。全程零网络、零真实模型/Provider，
未读凭证，未merge/push/tag/release。本检查点是本夜候选包，不声称独立审核通过。
