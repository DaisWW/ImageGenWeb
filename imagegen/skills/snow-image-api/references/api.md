# Snow AI Studio 个人生图 API

从 HTTPS 站点打开 **生图 API**，生成密钥后点击 **下载免配置 Skill**。解压 `snow-image-api-configured.zip` 并安装完整目录即可调用。免配置包携带该账号的密钥，只通过公司内部渠道分发。更换或关闭密钥会使旧包失效。

个人密钥在「生图 API」页面生成，之后可随时查看和复制。通用包需要将页面显示的站点地址和密钥分别配置为 `SNOW_IMAGE_API_URL` 和 `SNOW_IMAGE_API_KEY`。这两个环境变量须一起设置，可以覆盖包内配置。本机调试可用 HTTP。升级前创建的旧密钥仍可调用，更换一次后即可在页面查看。

下载并解压 **AI Skill**，把整个 `snow-image-api` 目录放入 AI 工具的 Skills 目录，或直接让 AI 读取目录中的 `SKILL.md`。内置脚本使用 Python 3.10 及以上版本的标准库。

## 接口

所有接口从站点地址拼接下表路径，使用 `Authorization: Bearer <SNOW_IMAGE_API_KEY>`。下载时也发送同一个请求头。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/v1/config` | 查询可用余额、支持的生图渠道和模型、各渠道能力、价格及请求限制。 |
| `POST /api/v1/generations` | 一次提交文生图、垫图编辑或局部重绘任务，返回 `job` 和指向查询接口的 `Location`。 |
| `GET /api/v1/generations/{job_id}` | 查询任务状态、进度、费用及每张图片的下载地址。 |
| `GET /api/v1/generations/{job_id}/images/{item_id}` | 下载该任务中生成成功的原图。 |

提交需要 `Idempotency-Key: <UUID>`。纯文生图使用 `Content-Type: application/json`，请求体最多 192 KiB。垫图或蒙版使用 `multipart/form-data`，其中 `payload` 是 JSON 字符串，`references` 是可重复的图片文件字段，`mask` 是可选的单个蒙版文件字段。每次新生成用一个 UUID。超时或连接中断后，用同一 UUID 和完全相同的参数及图片文件重试。首次接受返回 **202**，在任务记录保留期间，同一账号的重复提交返回 **200** 和原任务。使用同一 UUID 提交不同参数、图片或蒙版会返回 **409**。

垫图会自动把模式设为 `img2img`，没有垫图时使用 `text2img`。蒙版用于局部重绘，必须与唯一一张原图一起提交，且必须是与原图长宽比例一致的静态 PNG，最大 4 MiB。蒙版 alpha 小于 128 的透明区域会被重绘，alpha 大于或等于 128 的不透明区域用于保护。局部重绘沿用工作站限制，只支持 `sample` 策略。垫图数量、单图大小、总大小和模型侧限制可从 `config.limits` 与 `config.channels[].capabilities` 查询。

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
| `transparent_background` | 布尔值，默认 `false`。透明背景仅支持 PNG/WebP，且上游模型需支持。 |
| `moderation` | `auto` 或 `low`，默认 `auto`。`low` 仅支持 GPT Image 2 系列。 |
| `generation_stage` | 工作流阶段 `draft`、`refine` 或 `final`，默认 `final`。 |
| `generation_strategy` | `sample` 或 `explore`，默认 `sample`。局部重绘只支持 `sample`。 |
| `item_prompts` | 可选的逐张最终提示词列表。用于 `explore` 时必须提供，数量须等于 `batch_count`，张数为 2 到 `limits.max_exploration_images`。不需要对话模型接口。 |
| `creative_direction_id` | 可选的工作流元数据，默认 `auto`。 |

将参考图和蒙版文件放入同一个 multipart 请求。例如以下请求同时传递原图、生成参数和局部蒙版：

```powershell
curl.exe -X POST "$env:SNOW_IMAGE_API_URL/api/v1/generations" `
  -H "Authorization: Bearer $env:SNOW_IMAGE_API_KEY" `
  -H "Idempotency-Key: 0f84e20e-d621-4a28-a3f9-65d8ce64d31e" `
  -F 'payload={"channel_id":"REPLACE_WITH_CHANNEL_ID","prompt":"保留人物和构图，只重绘框选区域","size":"1024x1024","quality":"auto","output_format":"png","batch_count":1}' `
  -F "references=@source.png" `
  -F "mask=@mask.png"

python scripts/generate.py generate request.json --reference source.png --mask mask.png
python scripts/generate.py generate request.json --reference subject.png --reference style.png
```

每次创建新任务时生成新的 UUID 作为 `Idempotency-Key`。请求超时后重试时，复用原 UUID、参数及附件。

## 等待与下载

每隔约 3 秒查询 `job.status`，直到它为 `succeeded`、`partial`、`failed` 或 `canceled`。`job.items[]` 中生成成功的图片有 `download_url`，它是站点下的绝对路径。使用 Bearer 密钥请求该地址并保存图片。图片和任务按站点保留期清理。

费用以人民币字符串返回。`reserved_rmb` 是预占额，`charged_rmb` 是实际扣费。任务使用发放密钥的账号余额，按成功图片计费。**401** 表示密钥不可用，**402** 表示可用余额不足，**404** 表示当前密钥下找不到任务或图片，**409** 表示请求 ID 冲突。接口错误返回 `error` 和 `code`，部分失败详情可通过每张图片的 `error_code` 查看。
