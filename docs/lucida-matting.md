# Lucida 透明背景

勾选 **透明背景** 时：上游按普通不透明图生成；Worker 成功后调用 Lucida 抠图，再把带真实 Alpha 的结果入库计费。

## 启用方式（Docker 一体 + GPU）

1. 准备 Lucida 源码与权重到 `.tmp-lucida-src/lucida-main`（权重目录 `.model/lucida`）
2. 设置：

```env
LUCIDA_MATTING_URL=http://lucida:8000
LUCIDA_MATTING_MODEL=lucida
LUCIDA_MATTING_TIMEOUT_SECONDS=120
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
| `LUCIDA_MODEL_PATH` | 权重挂载源目录 | `./.tmp-lucida-src/lucida-main/.model/lucida` |
| `LUCIDA_TORCH_INDEX_URL` | torch 安装源 | `https://download.pytorch.org/whl/cu124` |

## 行为边界

- 透明背景 **不再** 向生图上游发送 `background=transparent`
- 未配置 `LUCIDA_MATTING_URL` 时，勾选透明背景的任务会失败（如 `matting_unavailable`）
- 默认 Compose profile 不含 Lucida；主站可单独启动

## 性能

默认 Lucida 镜像安装 **CUDA torch**，容器通过 `gpus: all` 使用宿主机 GPU。RTX 40 系上抠图通常可到亚秒～数秒；若无 GPU 可把 `LUCIDA_TORCH_INDEX_URL` 改为 CPU 索引并去掉 GPU 透传需求。
