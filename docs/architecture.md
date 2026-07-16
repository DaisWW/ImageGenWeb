# 架构说明

Snow AI Studio 是一个小型内部 Flask 应用。项目采用分层单体结构：一个可部署应用、一个 Worker 进程，以及清晰的模块边界，不引入分布式服务框架。

当前部署契约是单 Gunicorn Web 进程、单 Worker。登录限流、对话操作和工作站互斥保存在 Web 进程内；Worker 使用数据库租约拒绝第二个活跃实例。需要水平扩容前，必须先把这些协调状态迁移到共享存储，不能直接增加进程或副本数。

## 目录结构

```text
imagegen/
  app.py              Flask 应用工厂和进程级钩子
  config/             已校验、支持热刷新的渠道与聊天配置
  integrations/       兼容 OpenAI 的 HTTP 客户端和图片适配器
  services/           业务操作和事务边界
  web/                Flask 路由、鉴权和 HTTP 序列化
  models.py           SQLAlchemy 持久化模型
  serializers.py      数据库模型到公开 API 载荷的转换
  storage.py          图片校验和文件系统持久化
  worker.py           队列认领、上游执行和结算
config/                管理员配置保存前使用的兼容默认值
static/                按 css、js、图片和第三方资源组织的浏览器文件
templates/             基础模板、页面和共享局部模板
tests/support/         隔离应用、数据构造器和可复用上游测试替身
tests/integration/     按领域组织的业务与 HTTP 合同测试
tests/e2e/             跨组件浏览器流程与响应式回归
```

## 依赖规则

```text
web -> services -> models/storage
web -> serializers
services -> config、integrations
worker -> services、integrations
config -> repository/models
integrations -> config 中的值对象
```

- 服务层不导入 Flask 路由或请求全局对象。
- 集成层不提交数据库事务。
- 路由只校验 HTTP 结构，然后把业务决策交给服务层。
- API Key 只存在于加密配置存储和服务端配置对象中。
- `imagegen.services` 是稳定的服务导入入口；单个模块属于实现细节。

## 事务归属

- 账户、计费、工作站、会话和生成服务负责自己的数据库提交。
- Worker 维护自己认领任务的心跳，恢复孤立认领，并在结算前锁定用户、条目和任务。
- 生成队列准入通过数据库单行锁串行计数；用户名由数据库函数索引保证大小写不敏感唯一。
- 图片文件先写入，再提交对应数据库记录；回滚时删除文件。
- 删除与保留期清理先删除文件，再提交元数据删除；失败时保留元数据供下一次重试。
- 运行配置作为带版本文档保存，并使用旧值做原子版本校验。

## 扩展方式

新增图片渠道时，优先在 `imagegen/integrations/` 实现适配器，并在 `ProviderFactory` 中注册。渠道能力、价格、模型和并发属于管理员配置，不应写进路由。

新增用户流程时，先把业务操作加入对应服务，再在 `imagegen/web/` 暴露薄路由，并在 `tests/integration/` 覆盖服务和 HTTP 合同。

除非确实需要第二种持久化实现，不要围绕 SQLAlchemy 增加仓储抽象。当前服务就是事务边界，内部应用应保持易于追踪。
