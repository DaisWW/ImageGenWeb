GALLERY_URL = "https://gpt-image2.canghe.ai/"
AWESOME_REPOSITORY = "https://github.com/freestylefly/awesome-gpt-image-2"
SKILL_REPOSITORY = "https://github.com/wuyoscar/GPT-Image2-Skill"
COOKBOOK_GUIDE = (
    "https://github.com/openai/openai-cookbook/blob/main/examples/multimodal/"
    "image-gen-models-prompting-guide.ipynb"
)
COOKBOOK_EVALS = "https://github.com/openai/openai-cookbook/tree/main/examples/evals/imagegen_evals"


PROMPT_CRAFT_GUIDANCE = """提示词工程规范来自 OpenAI Cookbook、awesome-gpt-image-2 和 GPT-Image2-Skill：
1. 先写交付物与用途，再按画布/布局 → 主体/任务 → 环境/细节 → 约束组织；选择可维护的清晰格式，不堆砌关键词。复杂商品、食品或多系统画面可使用干净的 JSON/config 分区。
2. 精确文字逐条使用直引号，注明语言、大小层级、位置和可读性；不得补写用户未提供的品牌、价格、日期或文案。密集中文最终交付使用 high，并明确禁止乱码、拼音和额外英文。
3. 构图写明画幅、视点、景别、主体位置、负空间；材质、光线和配色分开描述。摄影只保留一个不冲突的拍摄语境，并加入可信的环境物件与现实瑕疵。
4. UI 写成产品规格；图表、技术图和科研图使用画布、网格/区域、节点、箭头、图例、单位和视觉编码；多面板写明数量、每格职责及跨面板一致性。
5. 文生图不得提及不存在的参考图。编辑与多参考图必须按编号说明每张图的角色、必须保留、必须改变和相互关系。
6. 编辑采用“只改变 X；其他保持不变”，重复身份、几何、文字、布局、镜头和品牌等关键不变量；当前工作流使用单点编辑，只有渠道与界面明确支持 mask 时才规划局部蒙版。
7. 负面约束只针对模型高概率犯错，保持短而具体；公共或商业输出优先原创主体，避免真实品牌、IP 和在世艺术家复刻。
8. 复杂任务先形成干净基线，后续每轮只改变一个问题。low 用于草稿探索，medium 用于方向精修，high 用于文字、密集信息、身份保持和最终交付。
"""


SOURCE_METADATA = (
    {"id": "openai-cookbook", "label": "OpenAI Cookbook", "url": COOKBOOK_GUIDE},
    {
        "id": "awesome-gpt-image-2",
        "label": "awesome-gpt-image-2 图库",
        "url": GALLERY_URL,
        "repository_url": AWESOME_REPOSITORY,
    },
    {"id": "gpt-image2-skill", "label": "GPT-Image2-Skill", "url": SKILL_REPOSITORY},
)
