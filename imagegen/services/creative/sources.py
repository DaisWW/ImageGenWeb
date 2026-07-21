from .gallery import GALLERY_ATLAS

GALLERY_URL = "https://gpt-image2.canghe.ai/"
AWESOME_REPOSITORY = "https://github.com/freestylefly/awesome-gpt-image-2"
SKILL_REPOSITORY = "https://github.com/wuyoscar/GPT-Image2-Skill"
SKILL_GAMING_GALLERY = GALLERY_ATLAS.source_url("gaming")
SKILL_CHARACTER_GALLERY = GALLERY_ATLAS.source_url("character-design")
SKILL_TECHNICAL_GALLERY = GALLERY_ATLAS.source_url("technical-illustration")
COOKBOOK_GUIDE = (
    "https://github.com/openai/openai-cookbook/blob/main/examples/multimodal/"
    "image-gen-models-prompting-guide.ipynb"
)
COOKBOOK_EVALS = "https://github.com/openai/openai-cookbook/tree/main/examples/evals/imagegen_evals"
PROMPT_CRAFT_GUIDANCE = """提示词工程规范来自 OpenAI Cookbook、awesome-gpt-image-2 和 GPT-Image2-Skill：
1. 先用 Gallery Atlas 选择一个最接近的类别；只有明确混合任务才选择 2～3 类。复用类别语法和 Case 结构，不复制其中的主体、品牌、IP、文字或未经提供的参考图职责。
2. 先写交付物与用途，再按画布/布局 → 主体/任务 → 环境/细节 → 约束组织；结构比形容词优先，不堆砌关键词。
3. 精确文字逐条使用直引号，注明语言、层级、位置和可读性；不得补写用户未提供的品牌、价格、日期或文案。密集中文明确禁止乱码、拼音和额外英文。
4. 画布、比例和固定布局写在主体之前。信息图、教育板和出版页面先定义标题、主体区、说明区、图例等固定版区，再描述视觉表面。
5. 商品、食品和多系统画面可使用干净的 JSON/config 分区；键描述环境、主体、材料、光线、动态细节和输出目标，不写实现代码或空泛质量词。
6. 科研图、数据图和技术图使用图表家族、面板、节点、列、栈、箭头、轴、单位、图例和视觉编码；重复面板必须共享比例尺、对齐和颜色语义。
7. UI 写成产品规格：设备画布、信息架构、组件状态、真实数据、精确文案、间距和图标对齐都必须明确，禁止只写“现代、干净、漂亮”。
8. 多面板先写准确数量和每格职责，再重复共享角色身份、服装、道具、色板、光线和世界规则；分镜还需指定景别、机位、轴线和叙事节拍。
9. 摄影只保留一个不冲突的捕捉语境，写明设备或视角、时地、自然光和现实瑕疵；加入 5～12 个可信环境物件比叠加“写实、电影感”更有效。
10. 风格锚点必须具体且有边界；材质、光线和配色分开控制。公开或商业输出优先描述媒介与制作特征，不用艺术家或工作室名称代替画面说明。
11. 海报、广告和活动视觉按产品/活动名、主张、信息模块、CTA、细则建立促销层级；只锁定用户提供的文字，并要求远距离可读。
12. 文生图不得提及不存在的参考图。编辑与多参考图按编号写每张图的角色、必须保留、必须改变和相互关系；采用“只改变 X；其他保持不变”，重复身份、几何、文字、布局、镜头和品牌不变量。
13. 当前工作流没有蒙版输入；只有渠道、接口和界面都明确支持 mask 时才规划局部蒙版，不得在普通垫图编辑中承诺 inpaint。
14. 负面约束只针对模型高概率犯错，保持短而具体，不能让大段否定词主导提示词。
15. 中文和多语言密集布局要给出全部逐字文案、语言、模块和层级；最终交付使用 high，并要求字符清晰、无错字、无额外语言。
16. 研究图只作为工作流草图或可复现视觉目标，不能把模型生成的图形或数值宣称为科学事实；真人、品牌、IP、安全与医疗内容保持原创、非误导和非露骨边界。
17. 复杂任务先形成干净基线，后续每轮只改变一个问题。low 用于草稿探索，medium 用于方向精修，high 用于文字、密集信息、身份保持和最终交付。
18. 游戏 UI 先锁平台、画布、屏幕状态、安全区和 HUD 分区；游戏原画先锁制作阶段、身份锚点、镜头动作和面板职责。两者都必须以 2～6 个可从最终图片判断的硬门槛收敛，不能用“参考某游戏”代替执行描述。
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
        "revision": GALLERY_ATLAS.revision,
        "gallery_index": GALLERY_ATLAS.index_url,
        "references": {
            "gaming": SKILL_GAMING_GALLERY,
            "character_design": SKILL_CHARACTER_GALLERY,
            "technical_illustration": SKILL_TECHNICAL_GALLERY,
        },
    },
)
