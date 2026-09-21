# 历史文档归档

本目录只保存历史，不是执行入口。当前目标和权限只看[CURRENT](D:/UGit/kth-irl-hybrid/docs/CURRENT.md)，开发文档导航见[docs/README](D:/UGit/kth-irl-hybrid/docs/README.md)。

本目录的`.gitattributes`禁用文本换行转换，保证Git中的归档原字节与SHA256清单一致，不依赖开发机的`core.autocrlf`设置。

## 归档内容

### 按当前方向的语义精简

[语义精简清单](D:/UGit/kth-irl-hybrid/docs/archive/20260918-semantic-cleanup/manifest.csv)保存三份退役文档及六份改写前快照的原路径、归档位置、SHA256和处理理由。

| 退出开发路径的文档 | 理由 |
|---|---|
| `docs/specs/business-contract-v1.md` | 旧R1子集及“不做六维/决定/报告”范围不适用AG1；原文仅供`contracts.py`/`audit.py`来源查证 |
| `docs/decisions/runtime-v3.md` | R0/R1开工与旧任务已结束；仍有效原则已保留在现行AGENTS和Spec |
| `docs/reference/飞书资料分析与当前实现建议.md` | 首次分析含已纠正结论和旧N1建议；当前材料事实查更正文与索引 |

旧版AGENTS的全部授权史、精简前CURRENT/导航/恢复输入均完整保存在该清单所列快照中。快照使用`.txt`后缀，避免被误当成自动加载的现行AGENTS。历史文件不会因归档而取得新授权。

以下首次归档的ZIP与原清单保持不变；查旧路径时，先查本次语义清单，再查首次归档清单，不需要通读两批材料。

### 首次归档

- [历史文档ZIP](D:/UGit/kth-irl-hybrid/docs/archive/20260918-pre-ag1.zip)
- [逐文件原路径、成员名与SHA256清单](D:/UGit/kth-irl-hybrid/docs/archive/20260918-pre-ag1-manifest.csv)
- 普通文件归档目录：`docs/archive/20260918-pre-ag1-files/`；CSV的`archived_file`给出每份文档的实际新路径。
- ZIP SHA256：`394723127cd03334ac4ee2e2c4db1ca904a25773cc6293b6b29226ce7d24d56c`
- 56份退役文档，另含4份本次精简前的导航/规格快照；共60个成员。

| 类别 | 数量 | ZIP内成员位置 |
|---|---:|---|
| 历史检查点与交接 | 49 | `docs/checkpoints/`，包含35份Markdown和14份JSON |
| 旧N1方案与夜间计划 | 3 | `docs/plans/N1/`、`docs/superpowers/plans/2026-09-09-kth-local-product-overnight.md` |
| 产品复审及旧F1输入 | 2 | `docs/reviews/20260918-product-feasibility/` |
| 自包含收口记录与合同 | 2 | `docs/工作目录与收口说明.md`、`docs/specs/workspace-self-contained-v1.md` |
| 精简前导航/规格快照 | 4 | `context/AGENTS.md`、`context/docs/CURRENT.md`、`context/docs/README.md`、`context/docs/specs/agent-evaluator-delivery-v1.md` |

本归档来自2026-09-18精简前的实际工作区，包含当时尚未提交的文档更新，不是仅从HEAD导出。归档时逐成员解压到内存校验原字节SHA256、大小，并核对源前后身份。原文件的处置以清单`action`为准：

- `archive_loose_copy`：原文件移动至`archived_file`，正文与原字节保持不变，同时保存在本ZIP中。
- `context_snapshot_kept`：只保存精简前快照，原路径保留现行版本，不删除。

环境拦截了删除散落副本的操作，本轮未改用其他工具删除，改为纯文件移动归档；ZIP与普通归档文件都保留，文件删除数为0。

## 如何查历史

1. 在CSV的`original_path`按旧文件名或旧路径查找，读取`archived_file`或ZIP中的`archive_member`。普通退役文件的成员名就是原仓库相对路径。
2. 可直接静态读取ZIP成员；确需展开时，只展开到全新的`.local/tmp/`目录，禁止覆盖现行文档或自动执行其中命令。
3. 归档正文、行序、hash和历史结论不改写。旧相对链接以`original_path`所处的原目录解释；目标也已归档时，继续按CSV定位。历史绝对路径是当时身份，不代表现在应回旧仓取件。
4. `context/`中的CURRENT和AGENTS只是快照，不能恢复旧授权。以往PASS只证明其原版本和范围，FAIL也不能删去。

## 保留边界

原位保留：四份AG1主线、仍对应保留实现的方法说明、政策草案、来源索引、方法分歧台账和被M0校验清单固定的六份M0文档。保留理由见文档导航；不是要求加载它们的旧流程。没有重写`provenance/SHA256SUMS`或历史manifest来迎合迁移。

未处理：`.local/`原始材料、聊天、Case、实际审核日志、私有备份、旧仓/工作树和凭据。首次归档56份文档，后续语义精简再退出3份；选择依据是当前需求/实现/风险关联，不是字节重复。独有正文与原有失败记录没有丢弃。

后续不继续修改本ZIP。新的历史归档使用新身份并登记来源，不能覆盖本档案；文档归档不等于产品实施、真实调用或Git提交授权。
