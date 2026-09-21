# KTH 本地工作流冻结候选检查点（2026-09-15）

本轮只执行 Owner 采用的 2026-09-14 冻结输入：有限修复、隔离验证、两条演示和交接。实际开工 2026-09-15 15:53:50 +08:00，四小时截止 19:53:50 +08:00；提前完成即停止。此处是**执行方本地验证**，独立验收尚未进行。

## 代码与回归

- 分支 `codex/runtime-rebuild`；开工 HEAD `c8a3aa56e3b8fa0ec0e78f959bd4edd13f78136b`，原有两份未提交附件恢复变更先阅读、保存原 patch 后保留并修复。
- 最后代码提交 `c6f7b64bc996b2a88322e0bfda731ee47583b7ca`；本轮未 reset、push、merge、tag、发布或安装到主仓。
- 区分新版 alias 落库后硬退出的精确封账与 v4 原始 import/source 身份的合法重复导入；昨日校验失败留下的 planned/零 attempts 不伪造旧历史。新同 blob 多路径/MIME、先 unsupported 后 saved 及旧对象新增路径的精确 alias/source 关系均有红绿日志。
- 独立预提交快照固定四模块：114 passed，退出码 0；干净 Git 代码提交导出的 archive 上全套实际收集 912 项，**912 passed / 0 failed / 0 skipped**，退出码 0、2617.16 秒。`workflow` 与 `store` 的实际 import path 在同一 pytest 进程打印并断言为该 archive。全套 stdout SHA256 `a5489f85a2d05fbebe11a130c0de29cc1c8909e4f3752451040f48e326ae8e22`；stderr 为空文件。
- 真实门控环境中的 R1.6、R2A、night-fix3 各用途独立 Case 副本、两份独立 night base Case 及封存 session 只读变量均由 `full-suite-run.json` 记录，未指向原 Case 写入。
- 2026-09-14 组合抽查的一次跨 job BRL manifest 不一致 failed job 及 broken=[] trace 原证据保留；本轮干净 HEAD、补丁快照的独立 Case 串行复核及最终组合相关集通过。单次旧信号根因仍未定性，不删旧失败、不改写为业务不足。

## 演示与边界

- 演示 A：授权 BP 原附件 `0000.bin` SHA256 `d2d8a79d6b5202e6b15f7db44c5b14db6125ee025f880e510beab6d2df4284e8` 复制到本轮新 Case。现有 Plugin 启动器实际完成 `case create / intake add / project / job create / status / run / review list / export`；精确 job `JOB2::078d94c24cfec4761f304754e6d9ab900a8dc04e8925c781c25fee63f51fed7f` 停在 `awaiting_candidate_proposal`，provider_calls=0。公司级候选单元的原始限制仍在，未手填专业 findings。
- 演示 B：**合成材料与 manual_import**；现有 E4 测试夹具的第一子进程在 CRL 输出后硬退出（91），第二进程恢复（0），同一精确 CRL 输出复用，并运行 BRL/CRL/FRL/IPRL/TMRL/TRL 六维。job `JOB2::a8a7e2aade8cd641a046c0dda0df5ab29fb2fa84086d2e478d72763f65d8193f`，manifest `AGGMAN::d7ff551c82908bd65e2f744c752232f86a7c63296ed062d063bff19e77941825`，view `OFFLINE6::dc1d2669b0c4725f0fc5ae38002078a95393e81c14cf7ebaaa7f4bcda6ad1c72`；六个 trace 的 broken=[]，CLI 六维与 manifest/view trace、export 共九条命令退出 0。采集脚本前两次错误日志保留，不冒称真实六维专业分析。
- 审计反例：本版 alias 已落库后硬退出（91），import note 被改写，第二进程拒绝（1），业务对象不变、原任务保持 claimed 且只有一次 attempt；后处理脚本错误及最终成功日志同样保留。
- 原始八文件前后 SHA256 与昨日期望一致；night-fix3 的 1889 项相对路径、长度、逐文件 SHA256 前后一致。仅本轮新 Case 副本发生测试与演示写入。

**未完成/未授权**：自动从新材料提出候选、真实 Provider 专业复核、扫描 OCR/全部格式、宿主真实安装、正式 D1/D2、报告、投资决定及发布。两个 `runtime_provider` 队列入口仍明确拒绝封存为已完成。未联网、未读取凭证、未产生外部费用。下一真实自动化 Gate 只准备零预算单次授权表，不启动。

外部冻结证据和命令根：`D:\t\kth-local-freeze-20260915\run-20260915-1554`；最新机器交接 `handoff.json`、原 patch、原失败、各阶段日志、Case 副本与 SHA256 清单均在该根。此处不声称独立验收通过，交 fresh context 复核后停止。
