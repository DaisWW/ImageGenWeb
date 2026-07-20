GALLERY_URL = "https://gpt-image2.canghe.ai/"
AWESOME_REPOSITORY = "https://github.com/freestylefly/awesome-gpt-image-2"
SKILL_REPOSITORY = "https://github.com/wuyoscar/GPT-Image2-Skill"
SKILL_GAMING_GALLERY = (
    "https://github.com/wuyoscar/GPT-Image2-Skill/blob/main/skills/gpt-image/references/"
    "gallery-gaming.md"
)
SKILL_CHARACTER_GALLERY = (
    "https://github.com/wuyoscar/GPT-Image2-Skill/blob/main/skills/gpt-image/references/"
    "gallery-character-design.md"
)
SKILL_TECHNICAL_GALLERY = (
    "https://github.com/wuyoscar/GPT-Image2-Skill/blob/main/skills/gpt-image/references/"
    "gallery-technical-illustration.md"
)
COOKBOOK_GUIDE = (
    "https://github.com/openai/openai-cookbook/blob/main/examples/multimodal/"
    "image-gen-models-prompting-guide.ipynb"
)
PROMPT_CRAFT_GUIDANCE = """提示词工程规范来自 OpenAI Cookbook、awesome-gpt-image-2 和 GPT-Image2-Skill：
1. 先写交付物与用途，再按画布/布局 → 主体/任务 → 环境/细节 → 约束组织；选择可维护的清晰格式，不堆砌关键词。复杂商品、食品或多系统画面可使用干净的 JSON/config 分区。
2. 精确文字逐条使用直引号，注明语言、大小层级、位置和可读性；不得补写用户未提供的品牌、价格、日期或文案。密集中文最终交付使用 high，并明确禁止乱码、拼音和额外英文。
3. 构图写明画幅、视点、景别、主体位置、负空间；材质、光线和配色分开描述。摄影只保留一个不冲突的拍摄语境，并加入可信的环境物件与现实瑕疵。
4. UI 写成产品规格；图表、技术图和科研图使用画布、网格/区域、节点、箭头、图例、单位和视觉编码；多面板写明数量、每格职责及跨面板一致性。
5. 文生图不得提及不存在的参考图。编辑与多参考图必须按编号说明每张图的角色、必须保留、必须改变和相互关系。
6. 编辑采用“只改变 X；其他保持不变”，重复身份、几何、文字、布局、镜头和品牌等关键不变量；当前工作流使用单点编辑，只有渠道与界面明确支持 mask 时才规划局部蒙版。
7. 负面约束只针对模型高概率犯错，保持短而具体；公共或商业输出优先原创主体，避免真实品牌、IP 和在世艺术家复刻。
8. 复杂任务先形成干净基线，后续每轮只改变一个问题。low 用于草稿探索，medium 用于方向精修，high 用于文字、密集信息、身份保持和最终交付。
9. 游戏 UI / HUD 必须先写平台、目标画布、屏幕状态和安全区，再写 HUD 分区、锚点、层级、间距、图标尺寸、真实数值和短文案；明确世界层与 UI 层，禁止海报化、随机 Logo、乱码和越界控件。
10. 游戏原画 / 概念设计必须先写制作阶段和交付物，再写身份锚点、轮廓、镜头、动作、尺度、材质、色板和光线；多面板必须固定网格与每格职责，并重复角色、服装、装备和世界规则。白发、疤痕、单侧护甲等非对称特征还必须把角色左右映射到每个面板的观看方向。
11. 游戏任务按以下顺序收敛：概念/交付物 → 画布与布局 → 主体身份或信息架构 → 镜头/交互状态 → 材质与光线 → 精确文字 → 2～6 个可验收硬门槛。不要用“参考某游戏”代替可执行描述。
"""


SOURCE_METADATA = (
    {"id": "openai-cookbook", "label": "OpenAI Cookbook", "url": COOKBOOK_GUIDE},
    {
        "id": "awesome-gpt-image-2",
        "label": "awesome-gpt-image-2 图库",
        "url": GALLERY_URL,
        "repository_url": AWESOME_REPOSITORY,
    },
    {
        "id": "gpt-image2-skill",
        "label": "GPT-Image2-Skill",
        "url": SKILL_REPOSITORY,
        "references": {
            "gaming": SKILL_GAMING_GALLERY,
            "character_design": SKILL_CHARACTER_GALLERY,
            "technical_illustration": SKILL_TECHNICAL_GALLERY,
        },
    },
)
