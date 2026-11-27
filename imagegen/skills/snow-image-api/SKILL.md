---
name: snow-image-api
description: Generate or edit images through the user's Snow AI Studio API. Use when the user wants images generated, edited, tracked or downloaded through their configured Snow AI Studio account.
---

# Snow AI Studio 生图

免配置包自带连接信息，安装完整目录后直接运行脚本。通用包使用环境变量 `SNOW_IMAGE_API_URL` 和 `SNOW_IMAGE_API_KEY`，可在「生图 API」页面获取。脚本读取密钥并用于请求头，避免在对话、请求模板、命令参数和输出中显示密钥。

1. 运行 `python scripts/generate.py config` 查询可用渠道、模型、能力、价格、余额和请求上限。根据需求选择支持对应生成模式、画幅和蒙版能力的渠道。用户未指定渠道或模型时，选择符合能力要求且价格最低的选项，并遵守用户给定的费用范围。
2. 按 [JSON 模板](references/request.json) 创建 UTF-8 请求文件，填写 `channel_id` 和最终 `prompt`。用户未指定张数时生成一张。AI 自行整理提示词和逐张提示词，不调用对话模型 API。
3. 文生图运行 `python scripts/generate.py generate request.json`。垫图编辑在同一命令附加 `--reference <图片路径>`，多张垫图可重复该参数。局部重绘附加唯一原图和 PNG 蒙版，例如 `python scripts/generate.py generate request.json --reference source.png --mask mask.png`。图片和 JSON 参数由脚本合并到同一提交请求。
4. 脚本输出请求 ID，提交后输出任务 ID，再轮询并下载图片。默认保存到当前目录的 `snow-images`，用子命令前的 `--output-dir` 指定其他目录。部分成功时说明成功张数和失败情况。已有任务 ID 时，用 `python scripts/generate.py wait <job_id>` 继续查询和下载。提交结果未知时，使用原请求文件、相同图片和 `--request-id <uuid>` 重试同一次提交。

命令中的脚本路径相对于本 Skill 的目录。直接编写 HTTP 请求或排查状态时，读取 [API 说明](references/api.md)。
