"""kth_hybrid —— KTH IRL Evaluator 整体重建运行层。

R0+R1 首批（T01–T06）：可靠原件仓、事务台账、审计 trace、全量分层证据盘点、
资格判断与真实判据入口。本包不依赖原版 kth_irl 包；判据目录仅通过
``kth_hybrid.catalog`` 在隔离子进程中从批准 wheel 机械提取。
"""

__version__ = "0.1.0"
