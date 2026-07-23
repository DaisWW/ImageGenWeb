# Lucida 透明背景

勾选 **透明背景** 时：上游按普通不透明图生成；Worker 成功后调用 Lucida 抠图，再把带真实 Alpha 的结果入库计费。

详情页 **Lucida 抠图 / Lucida ZIP** 仍可作为对已有不透明结果的显式后处理下载入口。

## 启用方式

1. 准备 Lucida 源码与权重到 `.tmp-lucida-src/lucida-main`（权重目录 `.model/lucida`）
2. 设置：

```env
LUCIDA_MATTING_URL=http://lucida:8000
LUCIDA_MATTING_MODEL=lucida
LUCIDA_MATTING_TIMEOUT_SECONDS=120
```

3. 启动（含 Lucida profile）：

```powershell
docker compose --profile lucida up -d --build
```

| 变量 | 说明 | 示例 |
| --- | --- | --- |
| `LUCIDA_MATTING_URL` | Lucida 根地址 | `http://lucida:8000` |
| `LUCIDA_MATTING_MODEL` | `/remove?model=` | `lucida` |
| `LUCIDA_MATTING_TIMEOUT_SECONDS` | 读超时秒数 | `120` |
| `LUCIDA_MODEL_PATH` | 权重挂载源目录 | `./.tmp-lucida-src/lucida-main/.model/lucida` |

## 行为边界

- 透明背景 **不再** 向生图上游发送 `background=transparent`
- 未配置 `LUCIDA_MATTING_URL` 时，勾选透明背景的任务会失败（如 `matting_unavailable`）
- 已有真实 Alpha 的结果，详情页显式抠图会返回 `409`
- 默认 Compose profile 不含 Lucida；主站可单独启动

## 性能

默认 Docker 镜像为 **CPU Lucida**。大图可能数秒到十余秒；GPU 部署可后续改为 CUDA 镜像。
