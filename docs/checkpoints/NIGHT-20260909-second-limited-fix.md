# NIGHT-20260909二次限定整改检查点

日期：2026-09-09。状态：执行方二次限定整改候选完成，等待新的fresh-context Astra独立复验。
本文件仅记录本地执行事实，不声称独立验收通过。

## 授权与基线

- Owner在当前执行对话明确采用并授权：
  `D:\t\kth-night-limited-fix-review-20260909\给执行层的NIGHT二次限定修复输入.md`。
- 范围仅为五类剩余P1，不授权R2后续、网络、真实模型/Provider、D1/D2、报告、投资决定或发布。
- 分支：`codex/runtime-rebuild`；起始HEAD：`cdbc1270a2f68f6294c68af613e0747ed21aea71`。
- 测试提交：`3aad996b43afb8760cf58abb843e01da00218955`。
- 最后全绿代码提交：`96b7d1ba179c70d7469ab26b0b8fa9d3da3de34f`。

## 限定修复

1. CRL读时trace使用冻结criteria/reviews/scope重新执行批准求值器，比较完整dimension，并精确绑定
   SQLite的CaseBasis版本、scope、product status、input/result身份。CRL正文伪造会同时使trace和
   manifest view构建失败。
2. 六维view与每条evidence license均按严格字段集合和完整正文重算内容摘要；所有角色入口先重核
   view/license。权限文本与批准wheel canonical正则一致，evidence refs必须非空、去重且受许可。
3. BRL经营指标在写入边界拒绝非标准JSON的NaN/Infinity，内核再用有限实数检查二次防御；BRL
   rule version升为`kth-hybrid.brl.night.v3`。
4. `TRL9-C1`对`longitudinal_operation`和`independent_operation_record`统一要求至少两个独立用户
   与明确纵向期间；TRL rule version升为`kth-hybrid.trl.night.v3`。
5. TMRL新增逐人员/团队、CaseBasis与scope绑定的不可覆盖身份overlay。runner从封存原件解析同一
   记录内的subject/status/scope并冻结，trace重核记录与provenance；内核只接受
   `verified/probable/ok`，overlay仅决定证据可用性。TMRL rule version升为
   `kth-hybrid.tmrl.night.v3`。

FRL规则内核和IPRL v2未修改；CRL 13条规则及已验收结果未重写。

## 反例与验证

- 基线复现位于独立`cdbc127` worktree，日志显式打印实际导入路径：
  `D:\t\kth-night-second-fix-work-20260909\baseline-cdbc127\plugin\src\kth_hybrid\__init__.py`。
  同一套二次反例得到`25 failed / 1 passed`，exit 1。日志SHA256：
  `2266d6247078c609115fe440e1bab612495dad953354ff4cc31baf4b80787e1d`。
- 修复后同一反例：`26 passed / 0 skipped`，exit 0。日志SHA256：
  `5a9635ca66462af72d63dc06914a1d2119e0fe6b81499319aa023af98c637941`。
- NIGHT十模块真实门控：`206 passed / 0 skipped`，exit 0。日志SHA256：
  `05a2bbe79f2c28427db145d0937eedc00acd4e1a90fe0f032b8fe478af3922a4`。
- 最终全套：`526 passed / 0 skipped`，exit 0；仅既有ZIP重名warning。日志SHA256：
  `98caf60e663104c97c1bf311f9e4d8c53c9530a7926919af31a4a6854c6acd2d`。
- FRL/IPRL专项：`93 passed`；非CRL trace/manifest隔离窄测：`3 passed`。
- canonical三错误码保持为`BRL_WEAK_COMMITMENT_FOR_LEVEL`、
  `IPRL_RIGHT_STATUS_NOT_VERIFIED`、`TMRL_EXECUTED_AGREEMENT_REQUIRED`；角色canonical权限九项通过。

## 新候选Case

最终候选：`D:\t\kth-night-second-fix-final-cases-20260909\night-fix2`。

- 从第一轮只读Case逐文件复制；1,844个文件、70,793,646字节，源/目标复制前树SHA256均为
  `f3facdc5d8dfcc144981ed1f0ee1fa10105d52d66a8f5afd6e40dae837322e77`。
- 新Case数据库SHA256：`230181d54bb90f9517a921183a27c99c89d093e9715951d4e0bdce8c81dc68e6`。
- manifest：`AGGMAN::34cf170ccf9b60ec4e92ff75200f1a52987e75cab7354a2e6e1a616be2a7f2a7`；
  文件SHA256：`68aec751bccb36a05d211d05903bd0699ea85db5ba6a97e2dda0f7bb87950901`。
- view：`OFFLINE6::a8330048f05873a75f0b7e85a00da0e9c4bcf9e8baff6a41b9c494fad85c4eb3`；
  文件SHA256：`273b16640105a3f0eba3248b1cd8805319d58e2f2971937a50c56d6ea2d51f13`。
- 角色工件SHA256：`fd8825f9206ca088c61490782c5d46718debd17b49c780bf10f6f9ab5f56fc1a`。
- 最终快照：
  `D:\t\kth-night-second-fix-final-cases-20260909\night-fix2\audit\final-snapshot-night-second-fix.json`；
  SHA256：`523cc75477a8d75804ae45e117aa31e62087f48e84be9a5d85fe656c12b4f17a`。
- 六维trace均通过；manifest/view/role源码重建的对象与字节比较全部为true。
- BRL/TRL/TMRL新增v3结果，旧v2结果仍在数据库；IPRL v2、FRL v1、CRL v1结果ID保持。
- 五个非CRL维度review计数均为0；TMRL身份overlay计数为0；五维继续诚实为
  `insufficient/0级`，没有为真实Case造正例。

## 保护与停止

原`weijiu-r2b`、第一轮最终Case、R2-A.1、R2-A、R1.6及旧session文件的hash全部与开工值一致。
保护清单：
`D:\t\kth-night-second-fix-final-cases-20260909\night-fix2\audit\protection-hashes-night-second-fix.json`。

全程未联网、未读取凭证、未调用真实模型/Provider，未生成D1/D2、正式报告、投资决定或发布。
本批到此停止，交新的fresh-context Astra独立复验。
