# 背景透明化

生图阶段始终保存上游返回的原图，不调用抠图服务，也不会覆盖原图。用户可在图片详情页打开 **背景透明化**，勾选一个或多个模型并行处理，逐步查看候选结果，再选择最满意的一张作为最佳结果。

透明化候选与生成任务相互独立：候选失败不会改变原生成状态、原图路径或钱包结算。用户可以切换棋盘格、白色和黑色预览背景，单独下载 PNG，也可以把本轮成功候选打包下载为 ZIP。

## 启用方式（Docker 一体 + GPU）

1. 准备 Lucida 源码与权重到 `.tmp-lucida-src/lucida-main`（权重目录 `.model/lucida`）
2. 设置：

```env
LUCIDA_MATTING_URL=http://lucida:8000
LUCIDA_MATTING_MODEL=lucida
LUCIDA_MATTING_TIMEOUT_SECONDS=120
BACKGROUND_REMOVAL_CONCURRENCY=2
LUCIDA_IMAGE=snow-ai-studio-lucida:latest
# 默认 CUDA 12.4 torch；CPU 回退可设 https://download.pytorch.org/whl/cpu
LUCIDA_TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124
```

3. 一键部署（默认含 Lucida GPU 服务）：

```powershell
.\deploy-docker.cmd
```

脚本每次执行缓存感知构建，源码未变化时会直接复用 Docker 缓存；随后校验 CUDA、启动服务并等待 Lucida 模型就绪。
只有明确要复用当前镜像时，才运行 `.\deploy-docker.ps1 -Lan -NoBuild`。

需要：Docker Desktop 启用 NVIDIA runtime，本机有可用 NVIDIA GPU。

| 变量 | 说明 | 示例 |
| --- | --- | --- |
| `LUCIDA_IMAGE` | Lucida GPU 镜像 | `snow-ai-studio-lucida:latest` |
| `LUCIDA_MATTING_URL` | Lucida 根地址 | `http://lucida:8000` |
| `LUCIDA_MATTING_MODEL` | `/remove?model=` | `lucida` |
| `LUCIDA_MATTING_TIMEOUT_SECONDS` | 读超时秒数 | `120` |
| `BACKGROUND_REMOVAL_CONCURRENCY` | Worker 同时运行的透明化候选总数 | `2` |
| `LUCIDA_MODEL_PATH` | 权重挂载源目录 | `./.tmp-lucida-src/lucida-main/.model/lucida` |
| `LUCIDA_TORCH_INDEX_URL` | torch 安装源 | `https://download.pytorch.org/whl/cu124` |

## 模型配置

数据库尚无管理员配置时，`config/matting_models.yaml` 提供初始列表，Lucida 位于第一项。部署后在管理后台的 **背景透明化模型** 中维护顺序、服务地址、上游模型、超时和单模型并发；新模型追加到列表末尾。普通用户只会看到已启用且服务地址完整的模型。

当前模型适配器使用 Lucida 兼容协议：健康检查调用 `GET /ready`，处理调用 `POST /remove?model=...`。如接入不同协议，应先增加对应服务端适配器，不能只填写一个不兼容的 URL。

并行度同时受两层限制：`BACKGROUND_REMOVAL_CONCURRENCY` 控制 Worker 的候选总并发，管理后台的“单模型并发”限制同一模型的并发。生成任务拥有调度优先级，透明化任务不会占用生成计费或渠道并发。

## 行为边界

- 生成 API 中遗留的 `transparent_background` 字段会被忽略，新任务始终持久化为 `false`
- PNG、WebP 或 JPEG 原图都可提交透明化；成功候选统一保存为带真实 Alpha 的 PNG
- Worker 会拒绝没有真实 Alpha 或仍是烘焙棋盘格的候选，但只把该候选标记为失败
- 每个候选保存模型配置快照；管理员后续改名或改地址不会改变已排队任务
- 已有排队或运行候选时，管理后台不能停用或删除对应模型
- 未配置任何透明化模型时，生成仍正常工作，详情页只提示当前没有可用模型
- `/health` 会报告透明化服务状态，但透明化不可用不会使主站 readiness 失败
- 默认 Compose profile 不含 Lucida；主站可单独启动

## 性能

默认 Lucida 镜像安装 **CUDA torch**，容器通过 `gpus: all` 使用宿主机 GPU。RTX 40 系上抠图通常可到亚秒～数秒；若无 GPU 可把 `LUCIDA_TORCH_INDEX_URL` 改为 CPU 索引并去掉 GPU 透传需求。
