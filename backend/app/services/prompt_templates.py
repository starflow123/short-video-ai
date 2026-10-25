"""提示词模板：把「怎么写提示词」沉淀成可复用、可编辑的模板库。

参照 Jellyfish 的 ``prompt_templates`` 设计：
- 按分类（category）组织，前端左侧用两级分类树展示；
- 模板正文用 ``{{ 变量 }}`` 占位，渲染时替换；
- 支持少量条件块 ``{% if "关键词" in 变量 %}...{% else %}...{% endif %}``，
  用于按画风 / 题材动态切换负面提示词；
- 内置模板随迁移一起入库（``is_system=True``），用户可另存为自己的模板。

本模块只做「模板存取 + 渲染」，不感知具体厂商。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 分类定义
# 与前端左侧分类树一一对应：一级分组 → 二级分类。
PROMPT_CATEGORY_GROUPS: list[dict] = [
    {
        "key": "frame",
        "label": "首帧 / 尾帧 / 关键帧",
        "categories": [
            {"key": "frame_key_image", "label": "关键帧图片提示词"},
            {"key": "frame_head_image", "label": "首帧图片提示词"},
            {"key": "frame_tail_image", "label": "尾帧图片提示词"},
        ],
    },
    {
        "key": "video",
        "label": "视频生成 / 分镜",
        "categories": [
            {"key": "video_prompt", "label": "视频生成提示词"},
            {"key": "storyboard_prompt", "label": "分镜提示词"},
            {"key": "combined_prompt", "label": "综合提示词"},
        ],
    },
    {
        "key": "character",
        "label": "角色",
        "categories": [
            {"key": "actor_image_front", "label": "演员正面图片提示词"},
            {"key": "actor_image_other", "label": "演员侧面 / 背面图片提示词"},
            {"key": "character_image_front", "label": "角色正面图片提示词"},
            {"key": "character_image_other", "label": "角色侧面 / 背面图片提示词"},
        ],
    },
    {
        "key": "scene",
        "label": "场景",
        "categories": [{"key": "scene_image_front", "label": "场景图片提示词"}],
    },
    {
        "key": "prop",
        "label": "道具",
        "categories": [{"key": "prop_image_front", "label": "道具图片提示词"}],
    },
    {
        "key": "costume",
        "label": "服装",
        "categories": [{"key": "costume_image_front", "label": "服装图片提示词"}],
    },
]

CATEGORY_LABELS: dict[str, str] = {
    item["key"]: item["label"]
    for group in PROMPT_CATEGORY_GROUPS
    for item in group["categories"]
}

# 支持在模板里使用的变量说明（前端「变量」面板展示用）
VARIABLE_HINTS: dict[str, str] = {
    "description": "本张画面的主体描述（通常是分镜提示词）",
    "extra_details": "额外细节：资产外观、动作拍点、构图补充等",
    "visual_style": "项目视觉风格关键词",
    "style": "项目整体画面风格",
    "view": "镜头方向 / 视角（如：正面 / 45 度侧前方 / 俯视）",
    "view_angle": "视角描述（如：左侧面 / 背面）",
    "aspect_ratio": "画幅（如 9:16 竖屏）",
    "base_negative": "默认负面提示词",
}

# 负面提示词：中英双语，覆盖低质量、结构错误、文字水印等常见问题
DEFAULT_NEGATIVE = (
    "low quality, worst quality, blurry, deformed, bad anatomy, bad hands, "
    "missing fingers, extra limbs, fused limbs, extra arms, mutated hands, "
    "melting face, distorted body, transparent body, floating body, multiple heads, "
    "duplicate person, cloned character, twin, doppelganger, same person twice, "
    "poorly drawn face, bad proportions, "
    "watermark, text, logo, signature, overexposed, underexposed, plastic skin, "
    "lowres, jpeg artifacts, grainy"
)

# 道具定妆图必须「只有这件物品」：模型很容易因为描述里带动作而补一只手或一个人
PROP_SHEET_NEGATIVE = (
    "person, people, human, hand, hands, arm, wrist, face, body, silhouette, "
    "character, wearing, holding, action, scene, room, furniture, text, logo, watermark"
)

# 图生视频特有的失效模式：中途换场景、凭空多出人物、动作偏离时间轴
VIDEO_NEGATIVE = (
    "scene change, cut to another location, different room, new background, "
    "new person appearing, extra character, person disappearing, changing clothes, "
    "hairstyle change, lighting change, color tone shift, teleporting, jump cut, "
    "camera jumping to another angle, exaggerated motion, floating limbs, morphing face"
)

_FRAME_REQUIREMENTS = """画面要求：
- 超高细节，8K 分辨率，极致锐度与纹理，皮肤 / 布料 / 材质纹理清晰可见
- 电影级光影：主光方向明确，明暗过渡自然，色调统一
- 电影感景深，背景虚化自然，主体与背景分离干净
- 人体结构正确：五官对称、手指数量正确、肢体无粘连
- 构图稳定，主体位置明确，符合三分法或黄金分割
- 完美贴合{{ visual_style }}视觉语言与{{ style }}整体氛围，风格高度一致"""

_FRAME_NEGATIVE = """负面提示（必须避免）：
{{ base_negative }}
{%- if "动漫" in visual_style or "anime" in visual_style.lower() -%}
 bad anime, deformed anime, low quality anime, flat color, flat shading, 3d render, cgi
{%- else -%}
 cartoon, 3d render, cgi, illustration, painting, sketch, anime
{%- endif -%}
{%- if "古装" in style or "古代" in style -%}
 modern clothing, modern background, contemporary elements
{%- endif -%}"""

# ---------------------------------------------------------------- 内置模板
# 前 6 条与 Jellyfish 内置模板保持一致（同分类、同正文），后 3 条为本项目补充的
# 关键帧 / 视频 / 综合模板，全部标记为 is_system（不可删除、不可改名）。
DEFAULT_TEMPLATES: list[dict] = [
    {
        "category": "actor_image_front",
        "name": "演员正面图片提示词",
        "preview": "默认演员正面提示词",
        "is_default": True,
        "variables": ["description", "extra_details", "visual_style", "style", "base_negative"],
        "content": (
            "视觉风格：{{ visual_style }}\n画面风格：{{ style }}\n\n"
            "高质量电影级{{ visual_style }}人像摄影，超详细专业演员写真：\n{{ description }}\n\n"
            "镜头方向：正面\n{{ extra_details }}\n"
            f"{_FRAME_REQUIREMENTS}\n\n{_FRAME_NEGATIVE}"
        ),
    },
    {
        "category": "actor_image_other",
        "name": "演员侧面/后面图片提示词",
        "preview": "演员非正面视角",
        "is_default": True,
        "variables": ["view_angle", "extra_details", "visual_style", "style", "base_negative"],
        "content": (
            "视觉风格：{{ visual_style }}\n画面风格：{{ style }}\n\n"
            "高度一致的人物，身份保留。与参考图像中完全相同的人的肖像，100% 身份一致性，"
            "相同的面部结构，相同的发型，相同的服装，相同的妆容，相同的年龄和表情基础。\n"
            "视角完全切换为：{{ view_angle }}，从这个角度看到的同一人，正确的透视和轮廓，"
            "头部朝向自然符合该视角，无任何正面元素残留，无脸部扭曲\n"
            "演员姿态：站立挺直，中立姿势\n{{ extra_details }}\n"
            f"{_FRAME_REQUIREMENTS}\n\n{_FRAME_NEGATIVE}"
        ),
    },
    {
        "category": "character_image_front",
        "name": "角色正面图片提示词",
        "preview": "角色正面图片提示词",
        "is_default": False,
        "variables": ["extra_details", "visual_style", "style", "base_negative"],
        "content": (
            "视觉风格：{{ visual_style }}\n画面风格：{{ style }}\n\n"
            "高度一致的人物，身份保留。与参考图像 1 中完全相同的人的肖像，100% 身份一致性，"
            "相同的面部结构，相同的发型，相同的妆容，相同的年龄和表情基础。"
            "如果参考图像 2 存在，使用参考图像 2 的服装。\n"
            "演员姿态：站立挺直，中立姿势\n{{ extra_details }}\n"
            f"{_FRAME_REQUIREMENTS}\n\n{_FRAME_NEGATIVE}"
        ),
    },
    {
        "category": "character_image_other",
        "name": "角色侧面/背面图片提示词",
        "preview": "角色非正面视角",
        "is_default": False,
        "variables": ["view_angle", "extra_details", "visual_style", "style", "base_negative"],
        "content": (
            "视觉风格：{{ visual_style }}\n画面风格：{{ style }}\n\n"
            "高度一致的人物，身份保留。与参考图像中完全相同的人，100% 身份一致性，"
            "相同的面部结构、发型、服装、妆容、年龄与表情基础。\n"
            "视角完全切换为：{{ view_angle }}，正确的透视与轮廓，头部朝向自然，无正面元素残留，"
            "无脸部扭曲。\n角色姿态：站立挺直，中立姿势\n{{ extra_details }}\n"
            f"{_FRAME_REQUIREMENTS}\n\n{_FRAME_NEGATIVE}"
        ),
    },
    {
        "category": "prop_image_front",
        "name": "道具图片提示词",
        "preview": "高质量道具特写",
        "is_default": True,
        "variables": ["description", "view", "extra_details", "visual_style", "style", "base_negative"],
        "content": (
            "视觉风格：{{ visual_style }}\n画面风格：{{ style }}\n\n"
            "高质量电影级{{ visual_style }}产品摄影 / 道具特写，超详细真实质感：\n{{ description }}\n"
            "镜头方向：{{ view }}，经典产品 / 道具拍摄角度\n{{ extra_details }}\n"
            "画面要求：\n"
            "- 超高细节，8K 分辨率，极致锐度与纹理\n"
            "- 电影感浅景深，f/1.4 或 f/2.8，背景虚化明显\n"
            "- 真实材质表现：金属反光 / 皮革纹理 / 玻璃折射 / 布料纤维清晰可见\n"
            "- 无变形、无多余部件，结构准确\n"
            "- 优秀构图，主体居中或三分法，背景服务于主体\n"
            "- 完美贴合{{ visual_style }}视觉语言与{{ style }}整体氛围\n\n"
            f"{_FRAME_NEGATIVE}"
        ),
    },
    {
        "category": "scene_image_front",
        "name": "场景图片提示词",
        "preview": "高质量电影场景",
        "is_default": True,
        "variables": ["description", "view", "extra_details", "visual_style", "style", "base_negative"],
        "content": (
            "视觉风格：{{ visual_style }}\n画面风格：{{ style }}\n\n"
            "高质量电影级{{ visual_style }}场景画面，超详细环境氛围：\n{{ description }}\n"
            "镜头方向：{{ view }}\n画面主体位置：画面正中\n{{ extra_details }}\n"
            "画面要求：\n"
            "- 超高细节，8K 分辨率，极致锐度与纹理\n"
            "- 电影感景深，f/2.8～f/5.6，前中后景层次分明\n"
            "- 真实光影与氛围：光源方向明确，雾气 / 雨丝 / 光斑 / 尘埃自然\n"
            "- 空间透视正确，建筑与物体结构无畸变\n"
            "- 优秀构图，引导线与层次感强\n"
            "- 完美贴合{{ visual_style }}视觉语言与{{ style }}整体氛围\n\n"
            f"{_FRAME_NEGATIVE}"
        ),
    },
    {
        "category": "costume_image_front",
        "name": "服装图片提示词",
        "preview": "高质量服装呈现",
        "is_default": True,
        "variables": ["description", "extra_details", "visual_style", "style", "base_negative"],
        "content": (
            "视觉风格：{{ visual_style }}\n画面风格：{{ style }}\n\n"
            "高质量电影级{{ visual_style }}服装特写 / 全身着装呈现，超详细布料质感：\n{{ description }}\n"
            "穿着方式：完整全身穿着\n{{ extra_details }}\n"
            "画面要求：\n"
            "- 超高细节，8K 分辨率，极致锐度与纹理\n"
            "- 电影感浅景深或中景深，f/1.8～f/4，突出服装质感\n"
            "- 真实布料纹理、车线、钮扣、金属配件、褶皱与磨损细节清晰可见\n"
            "- 人体结构正确，着装自然贴合，无穿模\n"
            "- 优秀构图，服装为画面主体或占 2/3 以上面积\n"
            "- 完美贴合{{ visual_style }}视觉语言与{{ style }}整体氛围\n\n"
            f"{_FRAME_NEGATIVE}"
        ),
    },
    {
        "category": "frame_key_image",
        "name": "关键帧图片提示词（通用）",
        "preview": "分镜关键帧通用模板",
        "is_default": True,
        "variables": ["description", "extra_details", "visual_style", "style", "aspect_ratio", "base_negative"],
        "content": (
            "视觉风格：{{ visual_style }}\n画面风格：{{ style }}\n画幅：{{ aspect_ratio }}\n\n"
            "电影级分镜关键帧，画面内容：\n{{ description }}\n{{ extra_details }}\n\n"
            "镜头与画面要求：\n"
            "- 明确的景别与机位，主体位置清晰，不在正中央呆立\n"
            "- 电影级光影：光位明确、明暗层次丰富、色调与全片统一\n"
            "- 超高细节，8K 分辨率，皮肤 / 布料 / 环境材质纹理真实可辨\n"
            "- 人物结构正确：五官对称、手指数量正确、肢体无粘连\n"
            "- 动作定格在「正在进行」的瞬间，而非完成后的静态摆拍\n"
            "- 环境氛围绕人物服务：符合该镜头的天气、时间与情绪\n"
            "- 完美贴合{{ visual_style }}视觉语言与{{ style }}整体氛围，与相邻镜头保持同一人物与色调\n\n"
            f"{_FRAME_NEGATIVE}"
        ),
    },
    {
        "category": "video_prompt",
        "name": "视频生成提示词（图生视频）",
        "preview": "图生视频运镜与动作描述模板",
        "is_default": True,
        "variables": ["description", "extra_details", "visual_style", "style", "base_negative"],
        "content": (
            "画面风格：{{ visual_style }} / {{ style }}\n\n"
            "以给定首帧为起点，生成一段连续、自然的镜头运动：\n{{ description }}\n{{ extra_details }}\n\n"
            "视频要求：\n"
            "- 镜头运动明确且稳定（推 / 拉 / 摇 / 移 / 跟），幅度适中，不抖动、不频闪\n"
            "- 主体动作连贯自然，符合物理规律，人物五官与服装全程保持一致，无变形、无闪烁\n"
            "- 首帧的人物位置、服装、光线与色调在整段视频里保持不变\n"
            "- 动作从首帧状态自然延续到结束，末尾留出下一镜头的衔接余地\n"
            "- 画面不出现字幕、水印、logo 或多余文字\n"
            "- 保持{{ visual_style }}与{{ style }}的视觉一致性\n\n"
            "负面提示（必须避免）：\n"
            "{{ base_negative }}, flickering, morphing, warping, jitter, frame drop, "
            "sudden cut, duplicated limbs, extra fingers, text overlay"
        ),
    },
    {
        "category": "combined_prompt",
        "name": "综合提示词（角色 + 场景 + 动作）",
        "preview": "把资产与镜头信息融合成一段完整提示词",
        "is_default": True,
        "variables": ["description", "extra_details", "visual_style", "style", "base_negative"],
        "content": (
            "视觉风格：{{ visual_style }}\n画面风格：{{ style }}\n\n"
            "【画面主体】{{ description }}\n"
            "【外观与细节（跨镜头保持一致，不得改写）】{{ extra_details }}\n\n"
            "【画面要求】\n"
            "- 明确景别、机位与运镜，构图稳定，主体不在画面正中呆立\n"
            "- 电影级光影，主光方向明确，色调与全片统一\n"
            "- 超高细节，8K 分辨率，材质与皮肤纹理真实\n"
            "- 人体结构正确，手指数量正确，无肢体粘连\n"
            "- 与相邻镜头保持同一人物、同一服装、同一场景与同一色调\n\n"
            f"{_FRAME_NEGATIVE}"
        ),
    },
]

# ---------------------------------------------------------------- 渲染
_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][\w]*)\s*\}\}")
_TAG_RE = re.compile(r"\{%\s*(.*?)\s*%\}", re.DOTALL)
_COND_RE = re.compile(r'^"([^"]*)"\s+in\s+([A-Za-z_][\w]*)(\.lower\(\))?$')


def extract_variables(content: str) -> list[str]:
    """取出模板正文里用到的变量名（按首次出现顺序去重）。"""
    seen: list[str] = []
    for match in _VAR_RE.finditer(content or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def _eval_condition(expr: str, ctx: dict[str, str]) -> bool:
    """只支持 ``"关键词" in 变量`` / ``"关键词" in 变量.lower()`` 两种写法。"""
    match = _COND_RE.match(expr.strip())
    if not match:
        return False
    needle, var_name, lower = match.group(1), match.group(2), bool(match.group(3))
    value = str(ctx.get(var_name) or "")
    if lower:
        value = value.lower()
    return needle in value


def render_template(content: str, variables: dict | None = None) -> str:
    """渲染模板：替换 ``{{ 变量 }}``，并按条件输出 / 隐藏 ``{% if %}`` 块。

    - 缺失的变量渲染为空串，不会报错；
    - 保留 ``{%- -%}`` 的空白裁剪语义，但用单个空格衔接，避免两段文本粘连。
    """
    text = content or ""
    ctx = {key: ("" if value is None else str(value)) for key, value in (variables or {}).items()}

    # {%- 去掉前面的空白；-%} 去掉后面的空白（换成一个空格保持可读）
    text = re.sub(r"\s*\{%-", "{%", text)
    text = re.sub(r"-%\}\s*", "%} ", text)

    out: list[str] = []
    conditions: list[bool] = []
    pos = 0
    for match in _TAG_RE.finditer(text):
        chunk = text[pos:match.start()]
        if all(conditions):
            out.append(chunk)
        pos = match.end()

        tag = match.group(1).strip()
        if tag.startswith("if "):
            conditions.append(_eval_condition(tag[3:], ctx))
        elif tag == "else":
            if conditions:
                conditions[-1] = not conditions[-1]
        elif tag == "endif":
            if conditions:
                conditions.pop()

    tail = text[pos:]
    if all(conditions):
        out.append(tail)

    rendered = "".join(out)
    rendered = _VAR_RE.sub(lambda m: ctx.get(m.group(1), ""), rendered)
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()
