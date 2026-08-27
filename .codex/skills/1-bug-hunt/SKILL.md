---
name: 1-bug-hunt
description: Use only when the user explicitly requests a full-project autonomous bug hunt for Snow AI Studio. Do not trigger for an ordinary bug report or a scoped review.
---

# Bug Hunt

仅在用户明确要求全项目 Bug 扫描时使用；普通故障和局部审查走常规工作流。默认先只读，
确认范围、预算和是否允许修复；依赖、运行数据和第三方模型默认跳过，不花费额度或修改生产数据。

## Workflow

1. 映射 Flask、权限、事务/模型、迁移、存储、Worker 租约/结算、渠道重试、前端和部署脚本。
2. 分批检查并记录范围；优先重复扣费/退款、丢文件、越权、密钥、迁移、重复 Worker、陈旧任务、SSRF 和重试分歧。
3. 按 P0/P1/P2/P3 报告触发条件、影响、文件/行号、证据和复现/推理。
4. 仅获授权后修复，并重跑最窄测试及受影响层的迁移/浏览器检查。
