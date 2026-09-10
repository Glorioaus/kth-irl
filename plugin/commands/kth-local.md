---
description: 操作 KTH 本地可恢复工作流候选
argument-hint: <子命令> --case-dir <目录> [参数]
---

使用插件自带启动器运行
`python "$CODEX_PLUGIN_ROOT/scripts/kth-local.py" $ARGUMENTS`，原样保留用户给出的
Case 路径和精确对象 ID。启动器从本插件 `src` 加载同一 CLI，不依赖全局
`kth-local` 或项目虚拟环境。不得选择 latest job，不得自动启用 simulated，
不得调用网络、真实 Provider。`export` 产物仅为非正式核验包，不是正式报告。
