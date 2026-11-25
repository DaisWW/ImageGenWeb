# Snow AI Studio 个人生图 API

在网页顶部打开 **API**，选择结果工作站，点击 **开通并生成密钥**。把页面显示的站点地址配置为 `SNOW_IMAGE_API_URL`，把本次生成的密钥配置为 `SNOW_IMAGE_API_KEY`。连接时使用该站点的 HTTPS 地址（本机调试可用 HTTP）。每个账号有一把有效密钥，更换或关闭会立即撤销旧密钥。

下载并解压 **AI Skill**，把整个 `snow-image-api` 目录放入 AI 工具的 Skills 目录，或直接让 AI 读取目录中的 `SKILL.md`。内置脚本使用 Python 3.10 及以上版本的标准库。

## 接口

所有接口从站点地址拼接下表路径，使用 `Authorization: Bearer <SNOW_IMAGE_API_KEY>`。下载时也发送同一个请求头。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/v1/config` | 查询绑定工作站、可用余额、渠道、默认模型、单张价格和请求限制。 |
| `POST /api/v1/generations` | 提交文生图任务，返回 `job` 和指向查询接口的 `Location`。 |
| `GET /api/v1/generations/{job_id}` | 查询任务状态、进度、费用及每张图片的下载地址。 |
| `GET /api/v1/generations/{job_id}/images/{item_id}` | 下载该任务中生成成功的原图。 |

提交需要 `Content-Type: application/json` 和 `Idempotency-Key: <UUID>`。JSON 请求体最多 192 KiB。每次新生成用一个 UUID。超时或连接中断后，用同一 UUID 和同一 JSON 重试。首次接受返回 **202**，在任务记录保留期间，同一工作站的重复提交返回 **200** 和原任务。使用同一 UUID 提交不同参数或换绑工作站后重发会返回 **409**。

## 请求模板

复制 [request.json](request.json)，把 `channel_id` 换成查询接口返回的渠道 ID，把 `prompt` 换成实际提示词。

| 字段 | 取值与默认值 |
| --- | --- |
| `channel_id` | 必填，使用 `config.channels[].id`。 |
| `prompt` | 必填，最大字符数见 `limits.max_prompt_characters`。 |
| `model` | 可选，省略时使用渠道的 `default_model`。 |
| `size` | 默认 `auto`，也可以指定如 `1024x1024` 的尺寸。宽高须为 16 的倍数，比例最多 3:1，像素数为 655,360 至 8,294,400，单边不超过 3840。 |
| `quality` | 默认 `auto`，支持 `low`、`medium`、`high`。GPT Image 2.5 系列可使用 `xhigh` 和 `max`。 |
| `output_format` | 默认 `png`，可从 `channel.capabilities.formats` 选择其他格式。 |
| `compression` | 整数 0 至 100，默认 90。 |
| `batch_count` | 整数，默认 1，上限见 `limits.max_batch_images`。 |

## 等待与下载

每隔约 3 秒查询 `job.status`，直到它为 `succeeded`、`partial`、`failed` 或 `canceled`。`job.items[]` 中生成成功的图片有 `download_url`，它是站点下的绝对路径。使用 Bearer 密钥请求该地址并保存图片。工作站也会显示这次任务，图片和任务按站点保留期清理。

费用以人民币字符串返回。`reserved_rmb` 是预占额，`charged_rmb` 是实际扣费。任务使用发放密钥的账号余额，按成功图片计费。**401** 表示密钥不可用，**402** 表示可用余额不足，**404** 表示当前密钥下找不到任务或图片，**409** 表示请求 ID 冲突。接口错误返回 `error` 和 `code`，部分失败详情可通过每张图片的 `error_code` 查看。
