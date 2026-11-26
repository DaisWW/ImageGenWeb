---
name: snow-image-api
description: Generate images from text through the user's Snow AI Studio API. Use when the user wants an image generated, tracked or downloaded through their configured Snow AI Studio account.
---

# Snow AI Studio 生图

免配置包自带连接信息，安装完整目录后直接运行脚本。通用包使用环境变量 `SNOW_IMAGE_API_URL` 和 `SNOW_IMAGE_API_KEY`，可在「生图 API」页面获取。脚本读取密钥并用于请求头，避免在对话、请求模板、命令参数和输出中显示密钥。

1. 运行 `python scripts/generate.py config` 查渠道、模型、每张价格、可用余额和张数上限。按用户指定的渠道或模型选择，未指定时可从支持需求的渠道中选择价格最低的一个。
2. 按 [JSON 模板](references/request.json) 创建 UTF-8 请求文件。填写具体的 `channel_id` 和最终 `prompt`，保留用户要求的画幅、张数和精确文字。用户未指定张数时生成一张。
3. 运行 `python scripts/generate.py generate request.json`。脚本先输出请求 ID，提交成功后输出任务 ID，再轮询并下载图片。默认保存到当前目录的 `snow-images`，用子命令前的 `--output-dir` 指定其他目录。
4. 根据实际状态给出图片路径。部分成功时同时说明成功张数和失败情况。已有任务 ID 时，用 `python scripts/generate.py wait <job_id>` 继续查询和下载。提交结果未知时，使用原请求文件和 `--request-id <uuid>` 重试同一次提交。

命令中的脚本路径相对于本 Skill 的目录。直接编写 HTTP 请求或排查状态时，读取 [API 说明](references/api.md)。
