---
name: 1-bug-hunt
description: Use only when the user explicitly requests a full-project autonomous bug hunt for Snow AI Studio. Do not trigger for an ordinary bug report or a scoped review.
---

# Bug Hunt

仅在用户明确要求全项目 Bug 扫描时使用；普通故障和局部审查走常规工作流。
依赖、运行数据和第三方模型默认跳过。

## 范围与授权

- 根据当前及此前用户消息确定范围、约束和已有授权，不重复询问已经明确的信息。
- 用户仅要求扫描或审查时，执行只读检查并报告发现。
- 用户明确要求扫描并修复时，直接完成范围内的最小修改及受影响验证。
- 未指定数值预算不阻止不产生外部费用的本地检查；已有预算必须遵守。
- 只有缺失信息实质影响范围、正确性、真实环境、费用或明确审批操作时才询问；
  仅暂停依赖答案的步骤，继续其他已授权工作。
- 不花费额度，不触碰生产数据、真实图片、余额或密钥。

## Workflow

1. 映射 Flask、权限、事务/模型、迁移、存储、Worker 租约/结算、渠道重试、前端和部署脚本。
2. 分批检查并记录范围；优先重复扣费/退款、丢文件、越权、密钥、迁移、重复 Worker、陈旧任务、SSRF 和重试分歧。
3. 按 P0/P1/P2/P3 报告触发条件、影响、文件/行号、证据和复现/推理。
4. 用户已要求修复时完成最小修复，并重跑最窄测试及受影响层的迁移/浏览器检查。
