# 工作仓自包含合同 v1

日期：2026-09-17。Owner在文档收口后明确要求补完新目录，尤其解除旧wheel和Case引用；本次允许必要的依赖加载、打包配置、离线测试和资料复制。不启动N1用途/专业复核业务，不调用Provider，不删除旧分支/工作树/记录。

## 验收范围

1. 正常catalog及其应用消费者只读取新包内校验过的固定判据数据，不运行或访问旧wheel/源码；方法内容、范围、资格规则不变。
2. 批准wheel、基线源码及必要来源参照在本仓私有参考区完整保存；旧路径仅作为来源身份，不是运行定位符。
3. N1具名三个Case及两份观察/响应原件均有新仓逐字节快照和前后稳定证明。原Case不以数据库方式打开，不改写旧记录中的来源文字；使用时从仓内保护快照再复制新工作Case。
4. 新进程中禁止网络和旧目录读取，实际加载目录、检查相关应用路径及新Case副本；构建包包含固定目录数据并验证异地加载。
5. 现行CURRENT和N1输入只指向新仓可用入口，旧材料身份留在映射台账。旧历史文档不改写为新发生的事实。
6. 本地提交当前收口成果；私有资料不入Git。保留可恢复校验包，不能把同盘校验包称为异地灾备。旧资产删除须单独具名授权。

## 变更与来源台账

| 路径/范围 | 理由与来源 | 验证 |
|---|---|---|
| `plugin/src/kth_hybrid/catalog.py` | 自有依赖加载改动；新固定数据读取/校验；旧提取函数仅为显式参考工具或兼容默认入口 | 无旧目录、无子进程加载；错误数据失败关闭 |
| `plugin/src/kth_hybrid/data/approved_catalog.v1.json` | 批准wheel六个registry getter的机械输出；SHA256与来源另录，不手写判据 | 180项内容与原机械提取一致，加载检查固定hash |
| `plugin/src/kth_hybrid/proposal_requests.py`、`audit.py` | 原消费者显式使用新加载器，不改业务合同 | 队列/审计相关回归 |
| `plugin/src/kth_hybrid/census.py` | 将本仓manifest默认位置改为由源码位置解析；不改旧manifest历史来源路径 | 异地源码位置解析 |
| `plugin/pyproject.toml`、`.gitattributes` | 将JSON加入包数据，防止Git换行转换破坏固定hash | 离线构建/异地安装布局加载、Git工件hash |
| `plugin/tests/test_catalog.py`及新增`test_workspace_self_contained.py` | 自有测试；独立目录加载、完整性/隔离/副本检查 | 先红后绿及受影响既有测试 |
| `AGENTS.md`、`README.md`、`docs/CURRENT.md`、`docs/工作目录与收口说明.md`、`docs/plans/N1/`、本合同、`docs/reference/`新增迁移导航/台账 | 更新当前授权与真实位置，不重写历史方法批准 | 路径、hash、读者/独立代码检查 |
| `.local/reference/approved-baseline/` | 既有provenance中批准wheel/源码/方法材料/registry/原测试的具名副本；公司内部/原第三方归属不变 | 逐文件hash，原件不动，不加入运行sys.path |
| `.local/reference/project-state/` | 既有计划的三个具名Case、两份非凭据观察/响应及必要裁决证据 | 完整文件集/源前后/目标hash，无原库打开 |
| `.local/workspace-closure/20260917-144447/` | 自有迁移/核验脚本、日志、变更前快照、离线新副本、校验归档 | 可复核原始输出，不作为N1/G1通过 |

本表是本次增量来源登记，不回写M0历史manifest。额外外部内容不自动入库；不读取/输出凭据，遇凭据或重解析链接不盲目整树复制。用户已批准的资料来源不因此成为合格业务证据。

## 停止与交付

wheel hash不符、源Case不稳定、固定数据与机械目录不一致、隐藏旧路径读依赖或跨界写入均阻断“自包含完成”。缺备份或存在独有历史分支成果，不宣称旧目录可删。无网络/Provider，测试HTTP仅用已有离线替身，不扩N1。

保留原验收身份，明确本次改变了catalog加载链，不能把9月15日裁决套到新提交。N1仍需Owner明确离线开工及其G1/G2/G3。
