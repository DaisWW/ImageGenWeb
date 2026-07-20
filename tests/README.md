# 测试约定

测试按“最窄的稳定边界”组织，新增需求不默认在每一层重复覆盖。

## 目录

- `support/platform.py`：创建隔离应用、数据库、默认用户，以及常用上游测试替身和数据构造器。
- `integration/test_*.py`：按业务领域覆盖服务行为和 HTTP 合同。
- `e2e/fixtures.js`：浏览器登录、工作站生命周期和响应式面板操作。
- `e2e/*.spec.js`：只保留必须经过真实页面交互才能证明的关键流程。

## 新增用例

1. 修复缺陷时，在能复现问题的最窄层增加一个回归用例。
2. 同一行为的多组输入使用表驱动循环，并用 `self.subTest` 标明输入，不复制测试方法。
3. 服务和 HTTP 只是同一条业务路径时，优先保留 HTTP 合同测试；只有事务、并发或失败恢复无法从 HTTP 观察时，才直接测试服务。
4. E2E 只覆盖跨组件交互、浏览器状态或响应式布局，不重复穷举后端校验。
5. 需要工作站、登录客户端、生成任务或上游响应时，先复用 `PlatformTestCase` 和 `e2e/fixtures.js`，再考虑增加 helper。

## 运行

```powershell
# 当前领域
.\.venv\Scripts\python.exe -m pytest tests/integration/test_generations.py -q

# 完整 Python 回归和覆盖率门槛
.\.venv\Scripts\python.exe -m coverage run -m pytest -q
.\.venv\Scripts\python.exe -m coverage report

# 浏览器核心流程：桌面全量，移动端只运行 @responsive
npm run test:e2e
```
