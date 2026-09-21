---
description: 通过插件内标准Skill组织KTH评估候选
argument-hint: <材料与评估问题，或明确Case/run的恢复请求>
---

读取并遵循同一插件的`skills/kth-irl-evaluator/SKILL.md`，将本次自然语言
请求与显式附件交给该Skill。命令只委托Skill，不另定义业务流程，也不把
用户文本直接拼入shell。没有新的真实运行许可时保持离线，不自动启用模拟。
内部工具始终使用同包`scripts/kth-local.py`；原样保留明确Case路径和run_id，
不选择latest、不加载全局另一版本。用户不需要手工准备JSON或操作CLI。
