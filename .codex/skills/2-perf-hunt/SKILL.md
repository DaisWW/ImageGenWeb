---
name: 2-perf-hunt
description: Use only when the user explicitly requests a full-project autonomous performance hunt for Snow AI Studio. Do not trigger for a single symptom or ordinary review.
---

# Perf Hunt

仅在用户明确要求全项目性能扫描时使用；普通性能症状走常规工作流。确认目标、环境、预算和只读范围；
结论必须有查询量、队列深度、延迟、分配、执行计划、图片大小、浏览器或 GPU 样本等证据。

## Workflow

1. 映射序列化、SQL/锁、存储/缩略图、Worker 调度、渠道调用、前端轮询/渲染和 Docker 启动。
2. 分批检查并记录范围；优先 N+1/无界查询、重复解码、队列抖动、噪声日志、过度轮询和重复渠道工作。
3. 报告影响、置信度、证据和验证方法；不以增加副本或削弱锁、校验、重试、计费、存储安全和正确性换性能。
4. 仅获授权后做最小改动并复测。
