# 宿主合同 ag1.m1.v4 与材料范围 ag1.m2.v1

工具路径为插件根下`scripts/kth-local.py`。内部命令由Agent调用，Case/临时输入必须在本次许可位置；路径不要写成开发机固定绝对路径。

## 提交

`agent prepare`输入精确字段：

- `schema_version`：`ag1.submission.v1`。
- `question`：用户行动问题，非空。
- `attachments`：1至256个显式绝对路径；提交冻结原件字节及失败项，覆盖状态须另读materials，不等于事实核实。
- `mode`：`offline`或`host`。当前host返回G2授权阻断。
- `host`：`name=codex`、实际`version/session_id`和`capabilities`，后者为`file_read/code_execution/isolated_contexts/execution_records`四个布尔值。未知不得假填true。

JSON最大4MiB、深度24。错误字段、错类型和未知合同拒绝，不静默修正为业务不足。

可选`revision={parent_run_id,reason}`关联明确旧run，材料字节/范围变化产生新身份。

## 发现任务往返

`agent prepare-task --run-id ... --input ...`输入`role=scope_discovery`及`payload`对象（仅可选`instruction`字符串）。只创建范围发现候选，未确认范围不得派发专业角色。

`agent begin-task --task-id ... --input ...`输入`context_id/source_mode`；context最多512个UTF-8字节，当前仅离线simulated。此操作先落Journal再返回票据。同一任务不得重复派发。

`agent submit --task-id ... --input ...`输入：

- `schema_version=ag1.host-result.v1`；
- 票据原值`case_id/dispatch_id/task_id/run_id/input_digest/role/context_id/source_mode/token/started_at`；
- 带时区的`ended_at`、`execution_status=succeeded|failed|cancelled`；
- 成功的`output`为下方完整范围候选，或明确空`candidates=[]`及可选字符串列表`unknowns`，失败/取消必须null；
- `usage={input_tokens: 非负整数或null, output_tokens: 非负整数或null}`。

不得附加`verified`、人工资格或最终等级。`simulated`永远不是H层。宿主来源标签本身也不证明独立性；真实独立角色要由适配者保留实际工具执行与不同上下文证据，缺失记`independence_unverified`。

`agent advance --run-id ...`只消费已有封存，不调用外部。恢复读取同一库内的不可变输入/返回与Journal；仅凭输出文件存在不算成功。

跨进程派发可能保守记为unknown；只可接收同一票据的确切迟到返回并消费，不重派、不改旧Journal终态。status同时保留`journal_state`，本地封存不证明外部exactly-once。

## 范围候选与确认

发现输出为`{coverage_digest,units}`，coverage摘要取自materials。units为1至64条：
`unit_id/name/product/market/root_task/system_boundary/financing_entity/subject/disposition/absorbed_by/reason/citations`。
disposition仅included/excluded/absorbed/unresolved；absorbed_by在吸收时指向纳入单元，其余为null。
citations每条为`{segment_id,start,end,quote}`，字符偏移和原文必须精确对应该run片段。

确认输入为`candidate_digest/actor/confirmed_at/subject/evidence_cutoff/units/action/permissions`。
subject为`{legal_name,aliases}`；时间带时区；units保留所有候选ID及完整字段，
不得留未决或把其他主体纳入本主体。actor只记录确认贡献，不证明资格。

action可为null，否则字段为`action_id/deadline/resource_cap/responsible/prohibited`，
action_id固定`external_investment.approve_diligence_or_validation`，
resource_cap为`{amount,unit}`，prohibited至少含investment/payment/contract。
permissions必须为`{mode:offline,external_actions:false}`，不能借确认跳过G2。
