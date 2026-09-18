# kth-irl-hybrid

KTH评估器的唯一实施仓。首版交付**当前Codex上的插件，内嵌标准Skill**：
从显式附件组织研究与评审，生成可追溯候选报告。辅助代码和CLI是Agent内部工具，
不是独立Windows应用；先工程候选交付，再与同事/业务方验专业效果。

## 从这里继续

1. [CURRENT](docs/CURRENT.md)：唯一实际状态、权限和下一动作。
2. [交付Spec](docs/specs/agent-evaluator-delivery-v1.md)：目标、范围、需求和完成定义。
3. [实施与验收计划](docs/plans/AG1/交付实施与验收计划.md)：工作包、文件/来源和验收证据。
4. [新对话输入](docs/plans/AG1/新执行对话完整输入.md)：跨对话恢复。

全部文档的现行/参考/历史分类与维护规则见[文档导航](docs/README.md)；
工作纪律见[AGENTS](AGENTS.md)，原始需求和材料定位见[参考入口](docs/reference/README.md)。
本README不复制进度、权威表或验收结论。

## 文件归属

- `plugin/`：唯一产品实现与测试。
- `docs/`：现行规格、计划及只读历史，按文档导航分层。
- `baseline/`、`provenance/`：既有方法/来源基线，不随整理改写。
- `.local/`：私有原件、Case、运行证据和备份；Git忽略不等于可删除。

保留已有工程成果，按合同替换模块，不另建平行实现仓。旧wheel是历史行为参考，
不是正常运行依赖或专业正确性证明；方法改变仍需明确裁定。旧Case/原件只读，
新工件仅按获准范围在`.local/runs/`产生。N1、F1和旧夜间计划不是现行执行输入。
