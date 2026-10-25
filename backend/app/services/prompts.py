"""提示词模板：主题/世界观 → 编剧 → 分镜指令。

编剧与分镜的约束组织方式参照 Jellyfish：
- ``chains/agents/script_divider_agent.py``（剧本分镜师 system prompt）
- ``chains/agents/shot_frame_prompt_agents.py``（21 条强约束与
  “景别/机位/运镜 → 场景环境 → 主体人物/关键对象 → 动作状态 → 氛围情绪 → 风格收束”组织顺序）
- ``schemas/skills/shot_frame_prompt.py``（镜头字段表：景别/机位/运镜/氛围/情绪标签/动作拍点）
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

logger = logging.getLogger(__name__)

from app.services.prompt_templates import DEFAULT_NEGATIVE, VIDEO_NEGATIVE
from app.services.skills import (
    ShotSkill,
    resolve_skills,
    skills_fields,
    skills_instructions,
)

# ---------------------------------------------------------------- 编剧
SCREENPLAY_SYSTEM = """你是一名金牌竖屏短剧编剧兼导演。
你的任务是根据用户给出的主题 / 世界观 / 设定，创作一集可直接拍摄与生成的短剧剧本。
要求：
1. 只输出 JSON，不要输出任何解释、Markdown 代码块标记或多余文字。
2. 剧本采用中文剧本体：场景标题 + 环境动作描述 + 「角色名：台词」；
   台词要口语化、精炼，符合竖屏短剧的观看习惯。
3. 结构遵循「起（钩子）—承（相遇/冲突）—转（情绪高点）—合（余味）」，
   开场 3 秒内建立悬念，结尾留一个余韵或悬念钩子。
4. 人物设定要清晰：姓名、年龄、身份、性格各一句话；出场人物控制在 2-4 人。
5. 场景数量控制在 2-4 个，每个场景给出时间（日/夜）与地点。
6. 不要出现镜头术语编号、分镜表、字幕、水印等内容——那是下一步分镜的工作。
7. 中文文本里的引号一律使用中文引号「」或“”，禁止使用英文直引号（"），以免破坏 JSON 结构。"""

_SCREENPLAY_SCHEMA = """{
  "title": "本集标题（中文，12 字以内，有画面感）",
  "synopsis": "本集梗概（中文，80 字以内）",
  "characters": [
    {"name": "角色名", "age": "年龄", "role": "身份", "traits": "一句话性格/外形特征"}
  ],
  "script": "完整剧本正文（中文，含场景标题、动作描述与对白，多行文本）"
}"""


def build_screenplay_prompt(
    *,
    premise: str,
    style: str | None,
    aspect_ratio: str,
    episode_index: int = 1,
    target_seconds: int = 60,
    extra: str | None = None,
) -> str:
    """根据主题 / 世界观生成一集剧本。"""
    orientation = "竖屏" if aspect_ratio == "9:16" else "横屏" if aspect_ratio == "16:9" else "方形"
    extra_block = f"\n【额外要求】{extra.strip()}\n" if extra and extra.strip() else ""
    return f"""请根据下面的主题 / 世界观，创作第 {episode_index} 集短剧剧本。

【主题 / 世界观 / 设定】
{premise.strip()}

【画面风格】{style or "未指定，请给出统一且适合短剧的写实风格"}
【画幅】{orientation}（{aspect_ratio}）
【本集时长目标】约 {target_seconds} 秒（可拆为 6-15 个镜头，稍后由分镜环节决定）
{extra_block}
请严格按以下 JSON 结构输出（字段含义见注释，实际输出不要包含注释）：
{_SCREENPLAY_SCHEMA}"""


# ---------------------------------------------------------------- 角色档案
CHARACTER_PROFILE_SYSTEM = """你是一名影视造型指导，负责为短剧建立「角色档案」。
你的任务是从剧本里提炼出每个出场角色的稳定外观与固定服装，供后续所有镜头复用，
确保 AI 生成的每个镜头里都是同一个人。

要求：
1. 只输出 JSON，不要输出任何解释、Markdown 代码块标记或多余文字。
2. 覆盖剧本里所有有名有姓、有台词的角色，不要遗漏主角。
3. appearance（长什么样）必须具体到可复现：性别、年龄段、脸型、发型发色、眉眼特征、
   身高体型、肤色、是否有痣 / 疤 / 眼镜等辨识特征；不要写「很帅」「很普通」这类空话。
4. costume（穿什么衣服）必须写清一套贯穿全剧的固定服装：颜色、款式、材质、鞋子与配饰。
5. 同一角色的描述要能一字不改地贴到任意镜头的提示词里，不要带情绪或动作。
6. traits 用一句话概括身份与性格。
7. 中文文本里的引号一律使用中文引号「」或“”，禁止使用英文直引号（"），以免破坏 JSON 结构。"""

_CHARACTER_SCHEMA = """{
  "characters": [
    {
      "name": "角色名（必须与剧本一致）",
      "age": "年龄段（如 25 岁上下）",
      "role": "身份 / 职业",
      "appearance": "长什么样：性别、脸型、发型发色、眉眼、体型、肤色、辨识特征",
      "costume": "穿什么衣服：一套固定服装的颜色、款式、材质、鞋子与配饰",
      "traits": "一句话性格",
      "tags": ["外观标签1", "外观标签2"]
    }
  ]
}"""


def build_character_profile_prompt(
    *,
    script: str,
    style: str | None,
    aspect_ratio: str,
    known: Sequence[str] | None = None,
) -> str:
    """从剧本提炼角色档案（外观 + 固定服装）。"""
    orientation = "竖屏" if aspect_ratio == "9:16" else "横屏" if aspect_ratio == "16:9" else "方形"
    known_block = ""
    if known:
        names = "、".join(str(item) for item in known if str(item).strip())
        if names:
            known_block = f"\n【剧本中已出现的角色名（必须全部覆盖，姓名原样保留）】{names}\n"
    return f"""请根据下面的短剧剧本，为每个出场角色建立稳定的外观与固定服装档案。
{known_block}
【整体风格】{style or "未指定，请给出统一且适合短剧的写实风格"}
【画幅】{orientation}（{aspect_ratio}）

【剧本文本】
{script}

请严格按以下 JSON 结构输出（字段含义见注释，实际输出不要包含注释）：
{_CHARACTER_SCHEMA}"""


def parse_character_profiles(payload: dict) -> list[dict]:
    """把模型返回的角色档案清洗成统一结构（缺字段补空串，去掉无名项）。"""
    result: list[dict] = []
    for item in payload.get("characters") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        result.append(
            {
                "name": name[:200],
                "age": str(item.get("age") or "").strip(),
                "role": str(item.get("role") or "").strip(),
                "appearance": str(item.get("appearance") or "").strip(),
                "costume": str(item.get("costume") or "").strip(),
                "traits": str(item.get("traits") or "").strip(),
                "tags": [str(tag).strip() for tag in (item.get("tags") or []) if str(tag).strip()][:6],
            }
        )
    return result


def character_asset_prompt(profile: dict) -> str:
    """把角色档案压成一段稳定的「外观描述」，直接拼进生成提示词。"""
    parts = [profile.get("appearance") or "", profile.get("costume") or ""]
    text = "；".join(part.strip() for part in parts if part and part.strip())
    return text


# ---------------------------------------------------------------- 分镜
SCRIPT_BREAKDOWN_SYSTEM = """你是一名专业影视「剧本分镜师」，同时具备镜头帧提示词设计能力。
你的任务是把一集剧本拆解为可直接用于 AI 生成的分镜清单，并为每个镜头产出画面提示词。

【分层写法】提示词是分层编译的，各层各管一件事：
- 景别 / 机位 / 运镜 / 构图 / 氛围 / 情绪 → 写进各自的镜头语言字段；
- 地点 / 时间 / 天气 / 光线 / 人物站位 → 写进 start_state；
- 人物固定外貌与服装 → 由角色档案统一注入，正文不要重复；
- 风格、画质、负面提示、前后衔接 → 由系统追加。
因此 description / image_prompt / video_prompt 这三个正文只写「主体 + 此刻动作 + 神态 +
道具的当前物理状态 + 可见瞬态细节」，上面这些已由独立层负责的内容一律不要重复。

【语言】所有字段（含 image_prompt / video_prompt）一律使用中文，不要输出英文提示词。
图片模型（Seedream / Agnes / 即梦）原生理解中文，用中文描述画面更准。

【强约束】
1. 必须继承本集/项目统一的画面风格，信息不足时向项目风格收敛，不要自由发散。
2. 剧本中已出现的角色姓名、场景、道具、服装必须原样保留，不得翻译、改名或替换同义词。
3. 不要为了「写全信息」而平均罗列全部人物与道具；优先突出主角色、主场景、主动作。
4. 【镜头连续性】所有镜头是一条连续叙事，不是独立海报：
   - 上一镜头里人物的位置、朝向、服装、情绪，在下一镜头里必须保持一致；
   - 上一镜头结束时的动作 / 画面定格，要作为下一镜头的起始状态；
   - 同一场景内不随意变换光线、色调、时间，除非剧本明确写了时间跳转。
5. 【画面差异化】每个镜头的 image_prompt 必须描述该镜头独有的画面瞬间：
   - 禁止与其他镜头使用相同或仅替换主语的提示词；
   - 景别 / 机位 / 主体动作 / 情绪至少有一项与相邻镜头明显不同；
   - 10 个镜头 = 10 张不同的关键帧，不能 3 个镜头画成同一张。
6. 【角色一致性】角色的姓名、年龄、外貌、服装由系统从角色档案统一注入并锁定，
   image_prompt 里只写姓名 + 位置 + 动作 + 神态 + 道具，不要重复、改写或补充年龄、
   脸型、发型、服装，也不要写「25 岁东亚男性」这类概括外形。重复描述一旦与档案
   不一致（例如档案 18 岁却写成 25 岁），AI 会各取一半，反而换脸、变老。
7. 明确主体在画面中的相对位置与朝向，减少构图与轴线突变。
8. image_prompt 描述的是「镜头开始时/最具代表性的瞬间」：若动作是连续动作链，
   用「刚开始 / 尚未完成 / 被打断」这类未完成态表达，不要直接写成完成态或情绪爆发结果。
9. dialogue 保留中文原文台词，没有台词则留空字符串；对白必须符合当前剧情上下文，
   不要脱离剧本孤立写台词。
10. 分镜数量与用户要求一致，节奏紧凑；每个镜头都要给出景别、机位、运镜三项镜头语言。
11. 【画面细节】image_prompt 要写足可见细节：手部动作与受力、面部微表情、衣料与发丝的走向、
    材质纹理、道具的物理状态、人物与光 / 雨 / 灰尘 / 影子等环境的瞬态互动。
    不要写「8K / 高清 / 电影级画质」这类画质词，也不要写光位色温这类归属氛围字段的内容——
    它们由系统分层追加。目标是具体到可以直接拍，而不是「一个人在房间里」这种概括句。
12. 【负面提示】不要输出负面提示词字段，负面提示由系统统一追加，你只需保证正向描述足够具体。
13. 【主体绑定】每个镜头必须用 subjects 逐个人物列出：姓名、画面位置与朝向、此刻动作、面部神态、视线落点、手里拿的道具。必须写姓名，禁止用「他/她/它/两人」代替；同一镜头里谁拿伞、谁空手、谁站在哪必须明确，避免 AI 把道具分错人。
14. 【道具绑定】props 逐个列出镜头里出现或关键的道具：名称、持有者姓名、当前状态。道具归属必须与 subjects.holds 一致；没有道具就输出空数组。
15. 【动作时间轴】action_timeline 必须按镜头时长切分，带秒数，时间从 0 开始且**铺满整个 duration_seconds**（最后一个拍点的 end 必须等于时长）。
    拍点数量 = 时长除以 3 向上取整（8 秒写 3 个、10 秒写 4 个），每个拍点 2-3 秒。
    一个拍点只做「一套连贯动作」（一个主语 + 一个动宾结构，不超过 20 字），禁止用「然后 / 接着 / 再 / 之后」
    在同一拍点里串联多个动作，也不要写「听到声音后眼神微动嘴角下撇」这种复合变化；
    拍点与拍点必须首尾相接（上一拍点结束的姿势 = 下一拍点开始的姿势），
    且动作在该时段内真实可完成（2 秒只能「抬手」，不能「抬手＋起身＋走到门口＋开门」）。
    每个拍点都要写清动作、神态、视线和运镜。
16. 【首尾状态】start_state 记录镜头开始时的地点、日夜天气、主光方向、人物站位朝向；end_state 记录镜头结束时的位置、姿态、神态与道具状态。下一镜头必须从上一镜头的 end_state 自然承接。
17. 【视频提示词】video_prompt 以 action_timeline 为依据写动作过程：起始姿态 → 过程中的关键变化 → 收尾状态，
    每个阶段写清力度、速度、手部轨迹、衣料 / 头发 / 道具的惯性与环境反馈。
    不要写「0.0-1.5s：…」这类时间戳（时间轴由系统按秒注入），不要写运镜与景别，
    也不要重复固定外貌与环境静态事实；开头不要另起场景，默认全程在 start_state 的地点内完成。
18. 【图片提示词】image_prompt 定格在第一个动作拍点「刚开始 / 尚未完成」的瞬间，
    必须出现 subjects 的人物姓名，并写清位置、动作、神态与道具当前的物理状态；不要写完成态结果。
19. 【镜头语言一致】subjects 中的位置、朝向、动作必须与 composition、image_prompt、video_prompt、action_timeline 相互一致，不能出现「文字说在左、构图写在右」这种自相矛盾。
20. 【人数就是人数】image_prompt 里出现的人物必须与 subjects 一一对应：subjects 写 2 人，画面就只有这 2 人，
    不得写「台下学生」「背景路人」「围观同学」「模糊人影」这类未列出的次要人物。需要群众场面时，
    先把群众作为一个 subject（如 name=「三班学生」）写进 subjects，再在提示词里描述。
21. 【不要写画面内文字】AI 模型渲染中文必然出错（错字 / 英文 / 乱码），所以 image_prompt / video_prompt 里
    禁止要求出现任何可读文字：黑板字、标语、幕布 PPT、手机屏幕上的帖子内容、字幕、招牌都不行。
    要表达「屏幕上有一条回复」就写「屏幕亮起一条白色消息气泡，内容不可辨识」；
    要表达「黑板写着倒计时」就写「黑板一角留着粉笔擦过的浅灰痕迹」；用光色、图形、动作代替文字。
22. 中文文本里的引号一律使用中文引号「」或“”，禁止使用英文直引号（"），以免破坏 JSON 结构。
23. 【动作必须是人类能完成的】所有动作拍点都必须是真人演员能做出的正常人类动作：走、站、坐、转身、
    抬手、低头、后退、抓握、遮挡、奔跑、停下。禁止写「身体溶解 / 被雾气擦除 / 轮廓消失 / 透明化 /
    漂浮 / 穿墙 / 分身 / 肢体变形 / 头部旋转 / 四肢扭曲」这类需要特效才能实现的身体变化——
    AI 会把它们直接画成畸形人体。超自然只能通过三种方式表达：
    (a) 环境异象：雾、光、影、物体自行移动、墙面剥落；
    (b) 人物的正常反应：僵住、后退、遮脸、逃跑、屏住呼吸；
    (c) 镜头语言：突然推近、画面倾斜、失焦。
24. 【一镜一动作】每个镜头只安排一个主要动作，围绕一个主轴人物展开；其余人物只做「站着 / 坐着 /
    看向某处」这类静态姿态，不要同时安排多人复杂动作。道具保持静止或只做最简单的状态变化
    （门被推开一条缝、手机屏幕亮起），不要让道具自己变形、飞起、碎裂成不规则形状。
25. 【局部入画不要写姓名】需要「只露一只手 / 半截袖子 / 局部背影」这种人物只入画一小部分的镜头，
    subjects 与 image_prompt 里都不要出现该角色的姓名，改写「一只年轻男性的右手」这类无姓名描述；
    写了姓名系统就会把角色的全身参考图塞进画面，模型会画出两个同一个人。更稳妥的做法是把这些
    镜头直接写成纯物体特写（手机搁在床头柜上外放），subjects 留空。
26. 【核心异象必须占据画面主体】镜头里最重要的异常物（雾、肢体、裂缝、纯黑平面）必须写在
    image_prompt 开头，并明确它「占据画面中心与最大面积」，人物只能写成「画面下缘的小半个背影剪影」；
    把它塞在长句末尾或与人群并列，模型会优先画人而丢掉异象。描述门 / 窗 / 缺口外部时，
    要用正向句子写清「外面是什么」（例如「门外是一整片翻涌的青黑色雾气」），不要只写「没有走廊」。
27. 【先规划后拆镜】拆镜前先在 plan 里把剧本按叙事节奏切成 3-6 个段落：每段给出 name、shots（该段镜头数）、
    seconds（该段时长）、summary（一段话概括），并在 reasoning 里说明为什么这样分、节奏怎么安排。
    随后输出的 shots 必须严格按 plan 的段落顺序逐段展开：beats[].shots 之和必须等于实际输出的镜头总数，
    段落之间的情节推进顺序不得与 plan 矛盾。用户指定了镜头数量时以指定值为准；未指定时由你按情节密度、
    单镜 5-12 秒的可完成度自行决定总数（1-60），并把这个决定写进 plan。
28. 【正文长度】image_prompt 控制在 200-300 字，video_prompt 控制在 150-250 字，description 控制在 60-120 字。
    这 200-300 字是模型真正会执行的部分，只写「关键的那一个」细节：一个手部动作、一个眼神、一处材质瞬态。
    image_prompt / video_prompt 里禁止再写年龄、脸型、发型、服装、配饰——这些由系统按角色档案注入；
    也不要写风格、画质、景别、机位、运镜、时间戳，它们各自由独立层注入。
29. 【逐镜时长】duration_seconds 必须逐镜决定，在 5-12 秒之间按动作可完成度取值，禁止整集所有镜头
    统一写同一个秒数：单一动作 5-6 秒，动作+情绪转折 7-9 秒，走位带动作 10-12 秒。
30. 【逐镜分辨率】resolution 逐镜选择 "720p" 或 "1080p"：关键情绪、特写、需要后期推近的镜头用 1080p；
    过场、远景、快速切镜用 720p。不要所有镜头都写同一个值。

【输出格式】只输出 JSON，不要输出解释、Markdown 代码块标记或多余文字。"""

# 分镜 JSON 各字段的示例行（键名必须与 generation._shot_directives / Shot.directives 结构保持一致）
_SHOT_FIELD_LINES: dict[str, str] = {
    "order_index": '"order_index": 1',
    "title": '"title": "镜头小标题（中文）"',
    "script_excerpt": '"script_excerpt": "本镜头对应的剧本原文摘录（中文）"',
    "description": '"description": "画面内容描述（中文：主体 + 动作 + 神态 + 道具状态 + 瞬态细节）"',
    "dialogue": '"dialogue": "台词（中文，没有则空字符串）"',
    "shot_size": '"shot_size": "景别：远景/全景/中景/近景/特写/大特写"',
    "camera_angle": '"camera_angle": "机位角度：平视/俯拍/仰拍/过肩/低角度"',
    "camera_movement": '"camera_movement": "运镜：固定/缓慢推镜/拉镜/摇镜/跟镜/手持晃动"',
    "atmosphere": '"atmosphere": "氛围描述（中文，如：雨夜霓虹的温柔孤寂）"',
    "mood_tags": '"mood_tags": ["情绪标签1", "情绪标签2"]',
    "action_beats": '"action_beats": ["动作拍点1", "动作拍点2"]',
    "composition": '"composition": "构图与空间锚点（中文，说明主体位置、环境重心、朝向关系）"',
    "subjects": '"subjects": [{"name": "角色名", "position": "画面中的位置与朝向", "action": "此刻正在做的动作", "expression": "面部神态", "gaze": "视线看向谁/哪里", "holds": "手里拿的道具，没有则空字符串", "is_primary": true}]',
    "props": '"props": [{"name": "道具名", "owner": "持有者角色名", "state": "道具当前状态（如：合拢/撑开/滴水）"}]',
    "action_timeline": '"action_timeline": [{"start": 0.0, "end": 3.0, "action": "这一时段唯一的连贯动作", "expression": "神态", "gaze": "视线", "camera": "运镜"}, {"start": 3.0, "end": 6.0, "action": "下一个动作", "expression": "", "gaze": "", "camera": ""}, {"start": 6.0, "end": 8.0, "action": "收尾动作，end 必须等于 duration_seconds", "expression": "", "gaze": "", "camera": ""}]',
    "start_state": '"start_state": {"location": "地点", "time_of_day": "日/夜", "weather": "天气", "light": "主光方向与冷暖", "positions": "人物站位与朝向"}',
    "end_state": '"end_state": {"location": "地点", "positions": "动作结束时的位置与姿态", "expression": "结束时的神态", "props": "道具结束状态"}',
    "duration_seconds": '"duration_seconds": 8',
    "resolution": '"resolution": "720p 或 1080p，按本镜重要程度逐镜选择"',
    "image_prompt": '"image_prompt": "中文关键帧提示词（80-150 字）：姓名 + 位置 + 此刻动作 + 神态 + 道具状态 + 一处瞬态细节，不要写景别/机位/环境/外貌服装/风格/画质"',
    "video_prompt": '"video_prompt": "中文图生视频提示词（60-120 字）：起始姿态 → 关键变化 → 收尾，写力度/速度/惯性，不要写时间戳/运镜/景别/外貌"',
}

# 分镜 JSON 的字段顺序（与改造前保持完全一致）
_SHOT_FIELD_ORDER: tuple[str, ...] = (
    "order_index",
    "title",
    "script_excerpt",
    "description",
    "dialogue",
    "shot_size",
    "camera_angle",
    "camera_movement",
    "atmosphere",
    "mood_tags",
    "action_beats",
    "composition",
    "subjects",
    "props",
    "action_timeline",
    "start_state",
    "end_state",
    "duration_seconds",
    "resolution",
    "image_prompt",
    "video_prompt",
)

# 结构字段：始终输出，不受 skills 开关影响。
# subjects/props/timeline/state 是跨镜头一致性与时间轴提示词的事实来源，必须始终产出。
_SHOT_ALWAYS_FIELDS: frozenset[str] = frozenset(
    {
        "order_index",
        "title",
        "description",
        "dialogue",
        "subjects",
        "props",
        "action_timeline",
        "start_state",
        "end_state",
        "duration_seconds",
        "resolution",
    }
)


def _build_shot_schema(skills: Sequence[ShotSkill], *, include_plan: bool = True) -> str:
    """按启用的 skills 动态组装 shots 的 JSON 结构（字段名与顺序保持兼容）。

    ``include_plan=False`` 用于分段拆解：整集的方案已由用户确认，这一轮只回填本段镜头，
    少一个顶层 plan 也就少一分被单次输出上限截断的风险。
    """
    enabled = set(skills_fields(skills))
    lines = [
        " " * 6 + _SHOT_FIELD_LINES[name]
        for name in _SHOT_FIELD_ORDER
        if name in _SHOT_ALWAYS_FIELDS or name in enabled
    ]
    shots_body = ",\n".join(lines)
    head = (
        '{\n  "synopsis": "本集梗概（中文，80 字以内）",\n'
        if not include_plan
        else (
            "{\n"
            '  "plan": {\n'
            '    "reasoning": "为什么这样拆分镜、节奏如何安排（中文，120 字以内）",\n'
            '    "beats": [\n'
            '      {"name": "段落名", "shots": 3, "seconds": 24, "summary": "这一段讲了什么（中文）"}\n'
            "    ]\n"
            "  },\n"
            '  "synopsis": "本集梗概（中文，80 字以内）",\n'
        )
    )
    return (
        head
        + '  "shots": [\n'
        "    {\n"
        f"{shots_body}\n"
        "    }\n"
        "  ]\n"
        "}"
    )


def _clean_text(value: object, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)[:limit]
    return str(value).strip()[:limit]


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def normalize_subjects(value: object) -> list[dict]:
    """把模型输出的 subjects 清洗成可落库的稳定结构（保留姓名与动作绑定）。"""
    if not isinstance(value, list):
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"), 100)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(
            {
                "name": name,
                "asset_id": _as_int(item.get("asset_id")),
                "position": _clean_text(item.get("position"), 200),
                "action": _clean_text(item.get("action"), 300),
                "expression": _clean_text(item.get("expression"), 200),
                "gaze": _clean_text(item.get("gaze"), 200),
                "holds": _clean_text(item.get("holds"), 200),
                "appearance": _clean_text(item.get("appearance"), 600),
                "costume": _clean_text(item.get("costume"), 400),
                "is_primary": bool(item.get("is_primary")),
            }
        )
        if len(result) >= 8:
            break
    return result


def normalize_props(value: object) -> list[dict]:
    """把模型输出的 props 清洗成可落库结构；道具归属用于避免「伞换人拿」。"""
    if not isinstance(value, list):
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"), 100)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(
            {
                "name": name,
                "asset_id": _as_int(item.get("asset_id")),
                "owner": _clean_text(item.get("owner"), 100),
                "state": _clean_text(item.get("state"), 200),
                "description": _clean_text(item.get("description"), 500),
            }
        )
        if len(result) >= 8:
            break
    return result


def normalize_timeline(
    value: object, duration: float, action_beats: object = None
) -> list[dict]:
    """清洗动作时间轴；模型没给时间轴时，用 action_beats 按时长均分兜底。"""
    try:
        total = max(1.0, float(duration or 5))
    except (TypeError, ValueError):
        total = 5.0

    def _num(raw: object, default: float) -> float:
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    result: list[dict] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            start = max(0.0, min(total, _num(item.get("start"), 0.0)))
            # 起点等于总时长时收缩 0.1s，避免 end 溢出镜头时长
            if start >= total:
                start = max(0.0, total - 0.1)
            end = min(total, max(start + 0.1, _num(item.get("end"), start + 1.0)))
            action = _clean_text(item.get("action"), 400)
            if not action:
                continue
            result.append(
                {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "action": action,
                    "expression": _clean_text(item.get("expression"), 200),
                    "gaze": _clean_text(item.get("gaze"), 200),
                    "camera": _clean_text(item.get("camera"), 200),
                }
            )
    if not result and isinstance(action_beats, list):
        beats = [_clean_text(item, 400) for item in action_beats if _clean_text(item, 400)]
        if beats:
            step = total / len(beats)
            for index, beat in enumerate(beats):
                result.append(
                    {
                        "start": round(index * step, 2),
                        "end": round(min(total, (index + 1) * step), 2),
                        "action": beat,
                        "expression": "",
                        "gaze": "",
                        "camera": "",
                    }
                )
    result.sort(key=lambda item: (item["start"], item["end"]))
    # 模型常少写最后一段：把末拍点拉到镜头时长，避免视频后半段没有指令而自由发挥
    if result and result[-1]["end"] < total - 0.05:
        result[-1]["end"] = round(total, 2)
    return result[:8]


def normalize_state(value: object) -> dict | None:
    if isinstance(value, str):
        text = value.strip()
        return {"notes": text[:1000]} if text else None
    if not isinstance(value, dict):
        return None
    cleaned = {str(key)[:50]: _clean_text(item, 600) for key, item in value.items()}
    cleaned = {key: item for key, item in cleaned.items() if item}
    return cleaned or None


def parse_shot_context(item: dict) -> dict:
    """从分镜 JSON 提取结构化上下文，并对缺字段做最小兜底。"""
    try:
        duration = float(item.get("duration_seconds") or 5)
    except (TypeError, ValueError):
        duration = 5.0
    subjects = normalize_subjects(item.get("subjects"))
    props = normalize_props(item.get("props"))
    # 模型漏写 props 时，从 subjects.holds 反推道具归属，避免提示词里出现无主道具
    if not props:
        for subject in subjects:
            holds = subject.get("holds") or ""
            if holds:
                props.append(
                    {
                        "name": holds[:100],
                        "asset_id": None,
                        "owner": subject.get("name") or "",
                        "state": "",
                        "description": "",
                    }
                )
    return {
        "subjects": subjects,
        "props": props,
        "action_timeline": normalize_timeline(
            item.get("action_timeline"), duration, item.get("action_beats")
        ),
        "start_state": normalize_state(item.get("start_state")),
        "end_state": normalize_state(item.get("end_state")),
    }


def normalize_plan(payload: dict) -> dict | None:
    """清洗规划方案：既接受模型返回的 ``{"plan": {...}}``，也接受用户确认后回传的纯方案对象。"""
    raw = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
    if not isinstance(raw, dict):
        return None
    beats: list[dict] = []
    for item in raw.get("beats") or []:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"), 40)
        if not name:
            continue
        beats.append(
            {
                "name": name,
                "shots": max(0, _as_int(item.get("shots")) or 0),
                "seconds": max(0, _as_int(item.get("seconds")) or 0),
                "summary": _clean_text(item.get("summary"), 200) or None,
            }
        )
    reasoning = _clean_text(raw.get("reasoning"), 1200)
    if not beats and not reasoning:
        return None
    return {"reasoning": reasoning, "beats": beats}


def _as_pos_int(value: object, *, default: int) -> int:
    """方案里的段落镜数 / 秒数可能是字符串或缺失，统一收到正整数。"""
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _beat_line(beat: dict, index: int) -> str:
    """把一个叙事段落渲染成提示词里的一行方案描述。"""
    name = str(beat.get("name") or "").strip()
    detail = "；".join(
        part
        for part in (
            f"{beat.get('shots')} 镜" if beat.get("shots") else "",
            f"约 {beat.get('seconds')} 秒" if beat.get("seconds") else "",
            str(beat.get("summary") or "").strip(),
        )
        if part
    )
    return f"{index}. {name}：{detail}"


def build_breakdown_prompt(
    *,
    title: str,
    script: str,
    style: str | None,
    aspect_ratio: str,
    shot_count: int | None,
    characters: list[dict] | None = None,
    skills: Sequence[str] | None = None,
    target_seconds: int | None = None,
    plan: dict | None = None,
    duration_range: tuple[int, int] = (8, 12),
    segment: dict | None = None,
    order_start: int = 1,
    previous_shot: str | None = None,
) -> str:
    # 按启用的 skills 动态组装镜头指令规范与 JSON 字段（不传 skills 时默认全开，行为不变）
    resolved = resolve_skills(skills)
    instructions = skills_instructions(resolved)
    # 分段拆解时整集方案已确认，这一轮只回填本段镜头，不再重复输出 plan
    schema = _build_shot_schema(resolved, include_plan=segment is None)
    skill_block = f"\n【镜头指令规范（skills）】\n{instructions}\n" if instructions else ""

    orientation = "竖屏" if aspect_ratio == "9:16" else "横屏" if aspect_ratio == "16:9" else "方形"
    if shot_count:
        count_line = (
            f"请先按叙事节奏规划拆解方案，再把下面这一集短剧剧本拆解为 {shot_count} 个分镜，"
            "并为每个镜头给出镜头语言与提示词。"
        )
        diff_line = f"【差异化要求】这 {shot_count} 个镜头的 image_prompt 必须各不相同："
    else:
        count_line = (
            "请先读完剧本、规划拆解方案，再把它拆解为分镜，并为每个镜头给出镜头语言与提示词。"
            "镜头总数由你决定（1-60 个），依据是情节拍点密度与单镜 5-12 秒的可完成度。"
        )
        diff_line = "【差异化要求】所有镜头的 image_prompt 必须各不相同："

    target_block = (
        f"【成片目标时长】约 {target_seconds} 秒，镜头数量与单镜时长要能撑起这个时长。\n"
        if target_seconds
        else ""
    )
    plan_block = ""
    if plan:
        reasoning = str(plan.get("reasoning") or "").strip()
        beat_lines = [
            _beat_line(beat, index)
            for index, beat in enumerate(plan.get("beats") or [], start=1)
            if isinstance(beat, dict)
        ]
        plan_block = "\n【已确认的拆解方案（必须严格遵守）】\n"
        if reasoning:
            plan_block += f"拆解思路：{reasoning}\n"
        if beat_lines:
            plan_block += "分段：\n" + "\n".join(beat_lines) + "\n"
        plan_block += (
            "镜头必须按上面的段落顺序展开，每段镜头数与方案一致；\n"
            "若本次指定的镜头总数与方案不同，按相同段落结构等比调整，不得改变叙事顺序。\n"
        )
    # 分段拆解：整集一次输出容易被模型的单次长度上限截断（实测 12 镜只剩 8 镜、
    # 末镜字段全空），所以一轮只写一段，段与段之间用上一段末镜的状态接力。
    segment_block = ""
    if segment is not None:
        seg_index = _as_pos_int(segment.get("seg_index"), default=1)
        seg_total = _as_pos_int(segment.get("seg_total"), default=1)
        seg_shots = _as_pos_int(segment.get("shots"), default=shot_count or 1)
        seg_title = str(segment.get("name") or "").strip()
        seg_summary = str(segment.get("summary") or "").strip()
        seg_seconds = _as_pos_int(segment.get("seconds"), default=0)
        count_line = (
            f"这一集已由用户确认拆解方案，现在只拆第 {seg_index}/{seg_total} 段"
            f"「{seg_title}」，请只输出这一段应有的 {seg_shots} 个分镜。"
        )
        diff_line = f"【差异化要求】本段这 {seg_shots} 个镜头的 image_prompt 必须各不相同："
        segment_block = (
            f"\n【本段任务（只输出本段 {seg_shots} 个镜头）】\n"
            f"段落内容：{seg_summary or seg_title}\n"
            + (f"本段目标时长：约 {seg_seconds} 秒，请在本段内自行分配每个镜头的时长。\n" if seg_seconds else "")
            + f"order_index 从 {order_start} 开始连续编号，共 {seg_shots} 个；"
            "不要输出其他段落的镜头，也不要重新规划整集结构。\n"
        )
        if plan_block:
            segment_block += plan_block
        if previous_shot:
            segment_block += (
                f"【上一镜的结束状态（本段第 1 镜必须承接，保证画面接得上）】\n{previous_shot}\n"
            )
        segment_block += "本段最后一个镜头要把 end_state 写实，下一段会直接从它继续。\n"
        plan_block = ""

    char_block = ""
    if characters:
        lines = []
        for item in characters:
            if isinstance(item, dict):
                name = item.get("name") or ""
                # 角色档案（长什么样 / 穿什么衣服）优先原样带入，让分镜提示词直接锚定人物外观
                look = "；".join(
                    str(item.get(k)) for k in ("appearance", "costume") if item.get(k)
                )
                detail = "，".join(
                    str(item.get(k)) for k in ("age", "role", "traits") if item.get(k)
                )
                parts = [part for part in (detail, look) if part]
                lines.append(f"- {name}：{'，'.join(parts)}" if parts else f"- {name}")
            else:
                lines.append(f"- {item}")
        char_block = (
            "\n【已确认人物设定（姓名必须原样保留）】\n"
            + "\n".join(lines)
            + "\n\n【角色一致性】上面的人物年龄、外貌、服装由系统按角色档案统一注入到每条提示词，"
            "正文里只写「姓名 + 位置 + 动作 + 神态 + 道具」，"
            "不要再重复或改写年龄、脸型、发型、服装、配饰。\n"
        )
    plan_note = (
        ""
        if segment is not None
        else """【拆解规划（先规划后拆镜）】
- 先在 plan.reasoning 里写清拆解思路，再在 plan.beats 里把剧本切成 3-6 个叙事段落：
  每段给出 name、shots（该段镜头数）、seconds（该段时长）、summary（这一段的情节内容）。
- 随后输出的 shots 必须严格按 plan.beats 的顺序逐段展开；
  beats[].shots 之和必须等于实际输出的镜头总数，实际镜头数必须与上面的数量要求一致。
"""
    )
    return f"""{count_line}

【分集标题】{title}
【画幅】{orientation}（{aspect_ratio}）
【整体风格】{style or "未指定，请给出统一且适合短剧的写实风格"}
【单镜时长】每个镜头 duration_seconds 取 {duration_range[0]}-{duration_range[1]} 秒（图生视频按此长度生成），
action_timeline 的秒数必须铺满这个时长。
{target_block}{char_block}{skill_block}
【剧本文本（完整上下文，所有分镜必须基于此剧本）】
{script}
{plan_block}{segment_block}{plan_note}
{diff_line}
- 每个镜头对应剧本里不同的情节节点 / 情绪拍点，不要把同一张画面换个景别重复用；
- 相邻镜头之间要有动作承接（上一镜头结束的动作 = 下一镜头开始的动作）；
- 同一场景内的连续镜头必须写同一个 location，光线与色调也保持一致；
- 所有 image_prompt / video_prompt 一律用中文撰写。

请严格按以下 JSON 结构输出（字段含义见注释，实际输出不要包含注释）：
{schema}"""


# ---------------------------------------------------------------- 拆解前的分镜规划
SHOT_PLAN_SYSTEM = """你是一名短剧分镜统筹。用户给你一集剧本，你只做「拆几个镜头、每个镜头多长、为什么」的规划，
不要输出任何分镜内容、提示词或画面描述。

判断依据（按优先级）：
1. 情节拍点：每一次信息变化 / 情绪转折 / 场景切换，至少一个镜头；同一地点同一动作可以合并。
2. 节奏密度：短剧每 10-15 秒要有一个小转折或强情绪点，镜头数要能撑住这个密度。
3. 可完成性：AI 图生视频单镜 5-12 秒最稳，超过 12 秒容易变形；一个镜头只安排一个主要动作。
4. 首尾接力：相邻镜头要能靠「上一镜结束状态 = 下一镜起始状态」接上；转身 360° 这类动作要拆成两个机位。

输出要求：
- reasoning：150-250 字，说明为什么这样拆，必须提到剧本里的具体情节节点与节奏安排。
- beats：把剧本切成 3-6 个叙事段落，每段给出 name、shots（该段镜头数）、seconds（该段时长）、
  summary（一句话概括这一段讲了什么）；各段 shots 之和必须等于 shot_count。
- shot_count：镜头总数（1-60）。
- shot_seconds：建议的单镜时长（5-12 秒）。
- target_seconds：成片目标时长；用户没给就按 shot_count × shot_seconds 估算。

只输出 JSON，不要输出解释或 Markdown：
{
  "reasoning": "为什么这样拆、节奏如何安排",
  "shot_count": 12,
  "shot_seconds": 8,
  "target_seconds": 96,
  "beats": [{"name": "开场钩子", "shots": 3, "seconds": 24, "summary": "这一段讲了什么"}]
}"""


def build_shot_plan_prompt(
    *,
    title: str,
    script: str,
    style: str | None,
    aspect_ratio: str,
    shot_count: int | None = None,
    target_seconds: int | None = None,
) -> str:
    """拼装「先给规划建议」的提示词：只要求数量、时长与理由，不产出分镜内容。"""
    orientation = "竖屏" if aspect_ratio == "9:16" else "横屏" if aspect_ratio == "16:9" else "方形"
    constraints: list[str] = []
    if shot_count:
        constraints.append(f"用户希望拆成 {shot_count} 个镜头，按这个总数规划。")
    else:
        constraints.append("用户没有指定镜头数量，由你按情节密度决定。")
    if target_seconds:
        constraints.append(f"成片目标时长约 {target_seconds} 秒，镜头数与单镜时长要和它对齐。")
    return f"""请先读完下面这一集剧本，给出拆解规划建议，不要直接写分镜内容。

【分集标题】{title}
【画幅】{orientation}（{aspect_ratio}）
【整体风格】{style or "未指定，按写实短剧处理"}
【约束】{''.join(constraints)}

【剧本文本】
{script}"""


# ---------------------------------------------------------------- 前后镜头衔接
def build_continuity_block(
    *, prev_summary: str | None, next_summary: str | None
) -> str:
    """把相邻镜头的上下文拼成一段衔接说明；无相邻镜头时返回空串。

    用于单独生成 / 重绘某个镜头的关键帧或视频时，把「上一镜头 / 下一镜头」的
    画面与动作语境注入提示词，避免脱离上下文导致画面、人物、色调跳变。
    """
    if not prev_summary and not next_summary:
        return ""
    lines = ["【前后镜头衔接（保持画面连续性）】"]
    if prev_summary:
        lines.append(f"上一镜头：{prev_summary}")
    if next_summary:
        lines.append(f"下一镜头：{next_summary}")
    lines.append(
        "本镜头需与上一镜头保持人物、服装、场景、光线与色调一致，"
        "并为下一镜头留下自然的动作 / 情绪收束，避免硬切。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- 生成提示词拼装
# 生成侧（关键帧 / 视频）的提示词统一按「主体描述 → 人物/道具绑定 → 参考图对应 →
# 镜头语言 → 动作时间轴 → 首尾状态 → 资产一致性 → 画面质量与细节 → 镜头上下文 →
# 负面提示」的顺序拼装，让模型拿到的指令更完整、细节更足。


def build_subject_block(
    subjects: Sequence[dict] | None,
    props: Sequence[dict] | None = None,
    *,
    empty_shot: bool = False,
    compact: bool = False,
    lean: bool = False,
    skip_motion: bool = False,
) -> str:
    """把「谁在哪、在做什么、什么神态、手里拿什么」写成不可歧义的硬约束。

    ``compact=True`` 供图生视频使用：动作已经由时间轴逐拍描述，这里不再重复，
    把省下的预算留给场景与镜头语言。
    ``lean=True`` 把通用解释压成短句：同一段规则每张图都要付一次字数，
    模型真正执行的是姓名、人数、道具归属这三条。
    """
    if not subjects and not props and not empty_shot:
        return ""
    lines = (
        ["【本镜头主体与道具归属】"]
        if lean
        else ["【本镜头主体与道具归属（必须严格遵守，姓名不得替换或合并）】"]
    )

    def _cap(value: object, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) > limit:
            return text[:limit].rstrip("，,；;、 ") + "…"
        return text

    def _field(label: str, value: object, limit: int) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return f"{label}：{_cap(text, limit)}"

    ordered = sorted(
        [item for item in (subjects or []) if isinstance(item, dict)],
        key=lambda item: (not bool(item.get("is_primary")),),
    )
    for item in ordered:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        cap = 16 if lean else 10_000
        parts: list[str] = []
        if item.get("position"):
            parts.append(_field("位置/朝向", item["position"], cap))
        # 有时间轴时，动作 / 神态 / 视线已经在时间轴块里逐拍写过，正文层不再复述
        if item.get("action") and not compact and not skip_motion:
            parts.append(_field("动作", item["action"], cap))
        if item.get("expression") and not skip_motion:
            parts.append(_field("神态", item["expression"], 6 if lean else 10_000))
        if item.get("gaze") and not skip_motion:
            parts.append(_field("视线", item["gaze"], 6 if lean else 10_000))
        if item.get("holds"):
            parts.append(_field("手持", item["holds"], 10 if lean else 10_000))
        # 绑定了资产的主体，外观与服装以【资产参考】里的锁定描述为准（跨镜头一字不改）。
        # 这里再写一遍分镜自己生成的版本，两份措辞不一致时模型会各取一半，反而画崩。
        if item.get("asset_id") is None:
            if item.get("costume"):
                parts.append(f"服装：{item['costume']}")
            if item.get("appearance"):
                parts.append(f"外观：{item['appearance']}")
        lines.append(f"- {name}：" + "；".join(parts) if parts else f"- {name}")
    prop_lines: list[str] = []
    for item in props or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        detail = []
        if item.get("owner"):
            detail.append(f"持有者={item['owner']}")
        if item.get("state"):
            detail.append(f"状态={_cap(item['state'], 12) if lean else item['state']}")
        if item.get("description"):
            desc = _cap(item["description"], 20) if lean else item["description"]
            detail.append(f"外观={desc}")
        prop_lines.append(f"- {name}" + (f"：{'；'.join(detail)}" if detail else ""))
    if prop_lines:
        lines.append("【道具归属】")
        lines.extend(prop_lines)
    unowned_props = [
        str(item.get("name") or "").strip()
        for item in (props or [])
        if isinstance(item, dict)
        and str(item.get("name") or "").strip()
        # 分镜里「持有者=无」是常见的显式写法，不能当成有人持有，否则异象不会进【必须入画】
        and str(item.get("owner") or "").strip() in ("", "无", "none", "null", "-", "—")
    ]
    names = [
        str(item.get("name") or "").strip()
        for item in ordered
        if str(item.get("name") or "").strip()
    ]
    # 「群像学生 / 全班」这类资产是一个群体，不是一个角色：按人头数写死会把整班压成几个人。
    group_names = [
        name
        for name in names
        if any(hint in name for hint in ("群像", "众人", "人群", "全班", "学生们"))
    ]
    single_names = [name for name in names if name not in group_names]
    if lean:
        hand = "手指数量正确，不出现多余手臂。" if any(
            str(item.get("holds") or "").strip() for item in ordered
        ) else ""
        if names and group_names:
            parts = []
            if single_names:
                parts.append(f"{'、'.join(single_names)} 各 1 人")
            parts.append(f"{'、'.join(group_names)} 为群体")
            lines.append(f"【人数】{'；'.join(parts)}；不画分身与路人。{hand}")
        elif names:
            lines.append(f"【人数】仅 {'、'.join(names)} {len(names)} 人；不画分身与路人。{hand}")
        if empty_shot:
            lines.append("【空镜】不出现人、人影、剪影、手或身体部位。")
        elif unowned_props:
            lines.append(
                f"【异象入画】{'、'.join(unowned_props[:6])} 是道具，按描述画在画面里，不受人数约束。"
            )
        return "\n".join(lines)

    if names and group_names:
        parts = []
        if single_names:
            parts.append(
                f"{'、'.join(single_names)} " + ("各 1 人" if len(single_names) > 1 else "1 人")
            )
        parts.append(
            f"{'、'.join(group_names)} 是多人群体，人数按本镜头描述确定"
            "（教室坐满、走廊站成一排都算正常），群体里每一名学生都是不同的人"
        )
        lines.append(
            "【人数硬约束】画面中的人物：" + "；".join(parts) + "。"
            "每个单独角色只出现一次，不得出现同一角色的第二个分身、双胞胎、克隆体，"
            "也不得在背景里添加与主角长得相似的人。"
        )
    elif names:
        lines.append(
            f"【人数硬约束】画面中只有 {len(names)} 名人物：{'、'.join(names)}。"
            "每个角色只出现一次，不得出现同一角色的第二个分身、双胞胎、克隆体，"
            "也不得在背景里添加与主角长得相似的人。"
        )
    # 手持道具的镜头最容易多出一只脱离身体的手（实测成片里出现过第三只手）。
    # 只约束「数量与结构」，不能写「只画持物的那只手」——那会把合法的另一只手也压掉。
    if any(str(item.get("holds") or "").strip() for item in ordered):
        lines.append(
            "【手部结构】画面中每只手都必须有完整的手臂连到身体，五指数量正确；"
            "手的数量与上面 subjects 描述一致，不多不少，不出现脱离身体的手。"
        )
    if empty_shot:
        # 空镜（门、走廊、道具特写）最容易被模型自行补一个人；必须显式禁止所有人体痕迹
        lines.append(
            "【空镜硬约束】本镜头没有人物：画面中不得出现任何人、人影、剪影、倒影、"
            "手、脸或身体部位，只表现场景与道具本身。"
        )
    else:
        lines.append(
            "【绑定规则】谁拿道具由「手持 / 持有者」决定；不得把道具换给其他角色，不得出现清单外的道具。"
            + (
                "清单里的每个角色都必须出现：单独角色各 1 人，群体按分镜描述给足人数；"
                if group_names
                else "画面里的人物必须与上面清单一一对应、人数相等："
            )
            + "不得添加背景路人、同学、围观者、模糊人影；"
            "需要群众时只能由清单里已命名的人物来承担。"
        )
        if unowned_props:
            # 「伸出的肢体 / 漫进来的雾气 / 墙上的缺口」这类无人持有的异象会被模型当成多余的人体，
            # 连同上面的人数约束一起被抹掉，导致本镜头的核心异象消失；这里明确它是道具、必须入画。
            lines.append(
                f"【异象必须入画】{'、'.join(unowned_props[:6])} 属于道具而不是人物，不受人数约束限制，"
                "必须按描述完整画在画面里，并占据描述中指定的位置与面积。"
            )
    return "\n".join(lines)


def build_timeline_block(
    timeline: Sequence[dict] | None, *, kind: str, duration: float | None = None
) -> str:
    """把动作时间轴写成带秒数的指令；图片只取第一个拍点作为定格瞬间。"""
    beats = [item for item in (timeline or []) if isinstance(item, dict) and item.get("action")]
    if not beats:
        return ""
    total = duration or max((float(item.get("end") or 0) for item in beats), default=0)
    if kind == "image":
        first = beats[0]
        detail = [
            f"定格在 {float(first.get('start') or 0):g}-{float(first.get('end') or 0):g}s 的瞬间：{first['action']}"
        ]
        for key, label in (("expression", "神态"), ("gaze", "视线"), ("camera", "镜头")):
            if first.get(key):
                detail.append(f"{label}：{first[key]}")
        return "【本帧定格时刻】\n" + "；".join(detail)
    lines = [f"【动作时间轴（镜头总时长 {total:g} 秒，必须按秒执行）】"]
    for item in beats:
        action = str(item["action"]).strip().rstrip("。；;，,")
        detail = [f"{item.get('start', 0):g}-{item.get('end', 0):g}s：{action}"]
        for key, label in (("expression", "神态"), ("gaze", "视线"), ("camera", "运镜")):
            if item.get(key):
                detail.append(f"{label}：{item[key]}")
        lines.append("- " + "；".join(detail))
    lines.append(
        "【时间轴硬要求】严格按上述秒数推进，不要增加时间轴之外的新动作；"
        "每个时段完成该段写明的一组连贯动作，动作在时段内完成，结束时自然收住。"
    )
    return "\n".join(lines)


def build_state_block(
    start_state: dict | None,
    end_state: dict | None,
    *,
    compact: bool = False,
    lean: bool = False,
) -> str:
    """首尾状态：用于相邻镜头承接，避免下一镜重新开局。

    ``compact=True`` 时省略与独立衔接块重复的「连续性」总述，只留首尾状态与场景锚点。
    ``lean=True`` 再把每个值截到一句话：状态是锚点，不是画面描述。
    """
    def _pair(state: dict) -> str:
        parts = []
        for key, value in state.items():
            text = str(value or "").strip()
            if not text:
                continue
            if lean:
                text = text.split("。")[0]
                if len(text) > 16:
                    text = text[:16].rstrip("，,；;、 ") + "…"
            parts.append(f"{key}：{text}")
            if lean and len(parts) >= 3:
                break
        return "；".join(parts)

    lines: list[str] = []
    if start_state:
        lines.append("【镜头起始状态】" + _pair(start_state))
    if end_state:
        lines.append("【镜头结束状态】" + _pair(end_state))
    if lines:
        if not compact:
            lines.append("【连续性】本镜头从起始状态开始，在结束状态收住；下一镜头应从此状态继续。")
        location = str((start_state or {}).get("location") or "").strip()
        if location:
            if lean:
                lines.append(f"【场景锚点】始终在「{location}」内，不换地点。")
            else:
                lines.append(f"【场景锚点】整段画面始终发生在「{location}」，不得离开该地点或换到别处。")
    return "\n".join(lines)


def build_directives_block(directives: dict | None, *, compact: bool = False) -> str:
    """把分镜里已有的景别/机位/运镜/氛围/构图真正注入生成提示词。

    ``compact=True`` 只保留景别 / 机位 / 运镜 / 构图四项并限长，
    保证视频提示词超预算时镜头语言不会再被整块删掉。
    """
    if not directives:
        return ""
    if compact:
        labels = {
            "shot_size": ("景别", 12),
            "camera_angle": ("机位", 12),
            "camera_movement": ("运镜", 16),
            "composition": ("构图", 40),
            "atmosphere": ("氛围", 24),
            "mood_tags": ("情绪", 16),
        }
        parts: list[str] = []
        for key, (label, limit) in labels.items():
            value = directives.get(key)
            if not value:
                continue
            if isinstance(value, list):
                value = "、".join(str(item) for item in value)
            value = str(value).strip()
            if key in ("composition", "atmosphere"):
                value = value.split("。")[0].strip()
            if len(value) > limit:
                value = value[:limit].rstrip("，,；;、 ") + "…"
            parts.append(f"{label}：{value}")
        return ("【镜头语言】" + "；".join(parts)) if parts else ""
    labels = {
        "shot_size": "景别",
        "camera_angle": "机位/角度",
        "camera_movement": "运镜",
        "atmosphere": "氛围",
        "mood_tags": "情绪",
        "composition": "构图与空间锚点",
    }
    parts: list[str] = []
    for key, label in labels.items():
        value = directives.get(key)
        if not value:
            continue
        if isinstance(value, list):
            value = "、".join(str(item) for item in value)
        parts.append(f"{label}：{value}")
    return ("【镜头语言（必须执行）】" + "；".join(parts)) if parts else ""


def build_reference_count_check(references: Sequence[dict] | None, subjects: Sequence[dict] | None) -> str:
    """带连续性参考图时，在提示词末尾再数一次人头：模型最容易在这里把参考帧里的人多留一个。"""
    if not references or not any(str(item.get("kind") or "") == "continuity" for item in references):
        return ""
    names = [
        str(item.get("name") or "").strip()
        for item in (subjects or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    if len(names) < 2:
        return ""
    return (
        f"【人数复核】连续性参考图是本图之前一刻的同一场戏，里面的人就是这 {len(names)} 位："
        f"{'、'.join(names)}。本图必须恰好 {len(names)} 个人，"
        "参考图里谁坐着、谁站在哪里都不代表本图，凡与 subjects 描述冲突的姿态与位置一律改掉，"
        "不得把参考图里的某个人留在原位、又另画一个同一个人。"
    )


def build_reference_legend(
    references: Sequence[dict] | None, *, compact: bool = False, lean: bool = False
) -> str:
    """声明参考图编号与角色/道具的对应关系，防止脸和道具张冠李戴。

    ``compact=True`` 用于视频预算吃紧时：保留编号对应关系，把使用规则压成一句，
    宁可少一层解释，也不要把「谁是谁」丢掉或挤掉正文的动作。
    """
    if not references:
        return ""
    lines = ["【参考图对应关系（严格按编号使用，不得互换身份）】"]
    for index, item in enumerate(references, start=1):
        kind = str(item.get("kind") or "")
        name = str(item.get("name") or "").strip()
        purpose = str(item.get("purpose") or "").strip()
        if lean and kind in ("actor", "prop"):
            text = f"图{index}：{'角色' if kind == 'actor' else '道具'}「{name}」"
        elif kind == "actor":
            text = f"图{index}：角色「{name}」的定妆参考图"
        elif kind == "prop":
            text = f"图{index}：道具「{name}」的外观参考图"
        elif kind == "continuity":
            text = f"图{index}：上一镜头/上一张关键帧的连续性参考图"
        elif kind == "identity":
            text = f"图{index}：{name or '角色'} 已确定的既有形象（仅锁定长相）"
        else:
            text = f"图{index}：{name or '参考图'}"
        if purpose and not lean:
            text += f"（{purpose}）"
        lines.append("- " + text)
    if lean:
        lines.append("【参考图规则】按编号对应，不互换；只锁长相与服装，不复制背景站位。")
        return "\n".join(lines)
    if compact:
        lines.append(
            "【参考图规则】严格按编号对应角色与道具，不得互换、不得重复；"
            "参考图只锁长相与服装，不复制其背景与站位；画面人数以【人数硬约束】为准。"
        )
        return "\n".join(lines)
    lines.append(
        "【参考图使用规则】角色只能使用对应编号参考图中的脸型、发型、五官与服装；"
        "道具只能由 subjects 指定的持有者拿；不要让参考图里的人互换身份、交换服装或交换道具；"
        "参考图只用于锁定对应角色的长相与服装，不是要求把每张图都当成一个独立人物塞进画面："
        "画面人物数量以【本镜头主体与道具归属】和【人数硬约束】为准，"
        "给了几张角色参考图不等于要画几个人，也不得把同一角色画成两个分身；"
        "定妆参考图只提供长相与服装，严禁复制它的背景房间、家具、门窗、地板与布光，"
        "场景一律按【场景锚点】和画面描述来画；"
        "连续性参考图里出现的人，就是本镜头 subjects 里的同一批人：只参考它的人物长相、服装、"
        "场景、光线与色调，严禁保留它的站位与构图，更严禁把它的每个人再各画一遍——"
        "本镜头的人数只由【人数硬约束】决定；"
        "若某张参考图已注明用途（如「仅保持五官、服装按描述改」），以该张标注的用途为准。"
    )
    return "\n".join(lines)


_SHEET_MARKERS = ("角色定妆参考图", "道具标准参考图", "场景参考图")


def _strip_sheet_directives(text: str) -> str:
    """砍掉资产 prompt 里「定妆参考图」那半句。

    资产 ``prompt`` 字段同时存了外观描述和参考图自己的构图指令（三视图、灰色背景纸）。
    分镜提示词若把后半句带进去，模型会画成色卡图，或者把摄影棚背景纸搬进画面。
    """
    cut = len(text)
    for marker in _SHEET_MARKERS:
        index = text.find(marker)
        if index != -1:
            cut = min(cut, index)
    return text[:cut].strip(" 。；;，,、")


def build_asset_block(
    assets: Sequence[dict] | None, *, compact: bool = False
) -> str:
    """把关联资产（角色 / 场景 / 道具 / 服装）的外观描述拼成一致性段落。

    资产描述要求跨镜头一字不改地复现，这是保证「同一个人 / 同一个场景」
    在不同镜头里长得一致的关键。

    ``compact=True`` 用于预算吃紧时：每条外观截到 90 字，保留脸型、发型与主色调，
    细节交给参考图，不整块丢弃（丢掉就没人锁人物了）。
    """
    if not assets:
        return ""
    kind_labels = {"actor": "角色", "scene": "场景", "prop": "道具", "costume": "服装"}
    lines: list[str] = []
    for item in assets:
        name = str(item.get("name") or "").strip()
        detail = str(
            item.get("appearance")
            or item.get("prompt")
            or item.get("description")
            or ""
        ).strip()
        detail = _strip_sheet_directives(detail)
        if compact and len(detail) > 90:
            detail = detail[:90].rstrip("，,；;、 ") + "…"
        if not name and not detail:
            continue
        label = kind_labels.get(str(item.get("kind") or ""), "参考")
        if name and detail:
            lines.append(f"- {label}「{name}」：{detail}")
        else:
            lines.append(f"- {label}：{name or detail}")
    if not lines:
        return ""
    if compact:
        return (
            "【资产参考（外观以本段锁定，细节以参考图为准）】\n"
            + "\n".join(lines)
            + "\n与上文用词冲突时一律以本段为准。"
        )
    return (
        "【资产参考（必须严格保持一致，外观描述不得改写或替换同义词）】\n"
        + "\n".join(lines)
        + "\n本次生成中，上述资产的外观 / 材质 / 配色必须与描述完全一致，"
        "确保跨镜头是同一个人 / 同一个场景 / 同一件道具。"
        "上文任何描述（含镜头提示词里的外貌、服装、肤色用词）与本段冲突时，一律以本段为准。"
    )


def _video_quality_compact(aspect_ratio: str, *, empty_shot: bool = False) -> str:
    """视频默认使用的精简质量块：保留场景 / 人物 / 形体底线，允许按时间轴运动。"""
    if empty_shot:
        return (
            "【视频质量与连续性要求】\n"
            "- 同一场景：不切换、不新增人物或人体痕迹\n"
            "- 画面只表现场景与道具，变化按时间轴推进\n"
            "- 道具稳定，不自行飞起、变形或碎裂\n"
            f"- 光线与色调与首帧一致；不抖动、不跳切，无字幕水印；画幅 {aspect_ratio}"
        )
    return (
        "【视频质量与连续性要求】\n"
        "- 同一场景、同一批人物：不切换、不新增、不消失\n"
        "- 动作按时间轴推进；需要双手时双手同时动作，允许转身、走动、被拉拽；"
        "不出现第三只手或脱离身体的手臂\n"
        "- 画面持续有运动：人物按拍点发力、有起势与收势，镜头随动作轻微推近或摇移"
        "（镜头语言指定固定机位时保持固定），不要全程静止或只做轻微晃动\n"
        "- 人体结构正确：不溶解、不变形、不穿墙、不分身\n"
        "- 道具按剧情拿取移动，不自行飞起、变形或碎裂\n"
        f"- 五官、发型、服装、光线与色调与首帧一致；不抖动、不跳切，无字幕水印；画幅 {aspect_ratio}"
    )


def build_quality_block(
    *,
    kind: str,
    aspect_ratio: str,
    compact: bool = False,
    empty_shot: bool = False,
    lean: bool = True,
) -> str:
    """画面 / 视频质量与细节要求（补齐模型容易忽略的细节维度）。"""
    if lean:
        if kind == "video" and empty_shot:
            return (
                "【视频质量与连续性要求】同一场景，不新增人物或人体痕迹；变化按时间轴推进；"
                f"光线与首帧一致，无字幕水印；画幅 {aspect_ratio}"
            )
        if kind == "video":
            return (
                "【视频质量与连续性要求】同场景同人物，不新增不消失；动作按时间轴推进；"
                f"光线与首帧一致，无字幕水印；画幅 {aspect_ratio}"
            )
        return (
            "【画面质量与细节要求】"
            f"画幅 {aspect_ratio}；光影真实、结构正确、无文字"
        )
    if kind == "video":
        if compact:
            return _video_quality_compact(aspect_ratio, empty_shot=empty_shot)
        return (
            "【视频质量与连续性要求】\n"
            "- 【场景锁定】整段视频始终在起始状态指定的同一地点与空间内，不切换场景；"
            "不新增、不丢失人物\n"
            "- 【人物锁定】人物身份、五官、发型、服装延续首帧；位置与朝向按时间轴变化，"
            "允许转身、走动、被拉拽\n"
            "- 【动作准确】动作按时间轴逐段执行，起止清晰、幅度真实、符合物理规律；"
            "需要双手时双手同时动作，不需要的手不额外加戏；不出现第三只手或脱离身体的手臂\n"
            "- 【人类形体】人物全程保持正常直立人类形态：四肢数量与结构正确；"
            "禁止身体溶解、半透明化、漂浮、穿墙、分身、四肢变形、头部旋转、面部融化\n"
            "- 【道具稳定】道具只允许被人物正常拿取与移动，不自行飞起、变形或碎裂\n"
            "- 镜头运动按时间轴的运镜执行（推 / 拉 / 摇 / 移 / 跟），平稳、不抖动、不频闪、不突兀跳切\n"
            "- 首帧的光线方向、冷暖与色调贯穿整段视频，不中途变色；"
            "画面不出现字幕、水印、logo 或多余文字\n"
            f"- 保持与全片统一的视觉风格与色调，画幅为 {aspect_ratio}"
        )
    orientation = "竖屏" if aspect_ratio == "9:16" else "横屏" if aspect_ratio == "16:9" else "方形"
    if compact:
        # 图片预算吃紧时保留形体 / 文字 / 场景三条底线，光影细节交给正文描述
        return (
            "【画面质量与细节要求】\n"
            f"- 画幅 {orientation}（{aspect_ratio}），构图稳定、主体明确\n"
            "- 电影级光影：主光方向明确，明暗层次丰富\n"
            "- 材质纹理与边缘细节清晰，避免塑料感皮肤\n"
            "- 人体结构正确：五官对称、手指数量正确、无多余肢体\n"
            "- 禁止身体溶解、漂浮、穿透、四肢扭曲、面部融化\n"
            "- 画面内不出现任何可读文字、字母或数字\n"
            "- 地点与【场景锚点】一致，色调统一，避免过曝、欠曝"
        )
    return (
        "【画面质量与细节要求】\n"
        f"- 画幅 {orientation}（{aspect_ratio}），构图稳定，主体位置明确，构图不呆板居中\n"
        "- 电影级光影：主光方向明确，明暗层次丰富，避免平光与大平光\n"
        "- 超高细节：皮肤 / 布料 / 金属 / 木石等材质纹理真实可辨，边缘锐利\n"
        "- 人体结构正确：五官对称自然，手指数量正确，肢体无粘连、无多余肢体、无多余手指\n"
        "- 人物必须是正常直立的人形：禁止身体溶解、半透明化、漂浮、穿墙、四肢扭曲、头部旋转变形、面部融化\n"
        "- 画面内不出现任何可读文字、字母或数字：屏幕 / 黑板 / 招牌一律用不可辨识的色块与光斑表达\n"
        "- 地点必须与【场景锚点】一致，不要画成图书馆、办公室、公寓等其他室内\n"
        "- 色调与全片统一，避免过曝、欠曝、灰暗脏色与塑料感皮肤"
    )


# Agnes 视频服务端硬限制 style.prompt ≤ 2000 字符；但实测超过 800 字后靠后的约束
# 权重明显下降。正文收在 200-300 字、硬约束精简到 600-700 字。
# 实测（分段拆解后正文回到 150-200 字）带完整角色图例 + 相邻镜衔接的一镜是 1098 字，
# 1100 的预算会把 quality（畸变底线）整块挤掉，所以留到 1400：仍然远低于厂商 2000 上限，
# 但保证硬约束一层不丢。
MAX_VIDEO_PROMPT_CHARS = 1400

# 图片同理：正文 200-300 字 + 必要硬约束约 500-600 字效果最稳，
# 2200 字时后半段的负面词与镜头语言几乎不起作用；这里收到 900 作为兜底。
MAX_IMAGE_PROMPT_CHARS = 900

# base（video_prompt）里已经写成「0.0-3.0s：…」时，再拼一遍结构化时间轴就是同一批拍点写两份，
# 实测同一句重复 350 字，直接挤掉正文后两段。这里用秒数区间判断是否已自带拍点。
_BEAT_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*[-–~至]\s*\d+(?:\.\d+)?\s*s")

_VIDEO_NEGATIVE_COMPACT = (
    "deformed, bad anatomy, extra limbs, duplicate person, new person, "
    "scene change, text, watermark"
)

# 图片预算吃紧时只保留最容易翻车的几项：形体、多手、分身、画质、文字
_IMAGE_NEGATIVE_COMPACT = (
    "low quality, blurry, bad anatomy, bad hands, extra limbs, "
    "duplicate person, text, watermark"
)

_VIDEO_CONTINUITY_COMPACT = (
    "【前后镜头衔接】本镜头从起始状态开始、在结束状态收住；人物、服装、场景、"
    "光线与色调与首帧及相邻镜头保持一致。"
)


def build_negative_block(
    negative: str | None = None,
    *,
    kind: str = "image",
    compact: bool = False,
    lean: bool = False,
) -> str:
    """负面提示段落：默认使用内置中英关键词列表，传空串表示不追加。

    自定义负面词只做「追加」，不能替换内置列表——内置的 bad anatomy / extra limbs /
    deformed 是压住畸形人体与多余肢体的底线，一旦被替换掉，画面立刻出现非人类结构。
    视频另加一组「中途换场景 / 多出人物」的失效模式，图生视频最容易在这里崩。

    ``compact=True`` 用于预算吃紧时：视频只保留覆盖畸形 / 多手 / 分身 / 换场 / 文字的短清单，
    图片只保留形体 / 多手 / 分身 / 画质 / 文字的底线项，避免整段负面词把正文挤掉。
    """
    if negative is not None and not negative.strip():
        return ""
    if compact:
        if kind == "video":
            pool = _VIDEO_NEGATIVE_COMPACT
        else:
            pool = _IMAGE_NEGATIVE_COMPACT
        custom = (negative or "").strip()
        text = f"{custom}, {pool}" if custom else pool
        header = "【负面提示】" if lean else "【负面提示（以下内容禁止出现）】"
        return f"{header}\n{text}"
    if negative is None:
        parts = [DEFAULT_NEGATIVE]
    else:
        parts = [negative.strip()]
        if DEFAULT_NEGATIVE not in negative:
            parts.append(DEFAULT_NEGATIVE)
    text = ", ".join(parts)
    if kind == "video" and VIDEO_NEGATIVE not in text:
        text = f"{text}, {VIDEO_NEGATIVE}"
    return f"【负面提示（以下内容禁止出现）】\n{text}"


def _truncate_at_boundary(text: str, limit: int) -> str:
    """按句 / 分句边界截断，优先整句保留，实在没有边界才硬切；绝不留下半个秒数。"""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:1] + "…"
    window = text[:limit]
    min_keep = max(1, int(limit * 0.6))

    def with_ellipsis(value: str) -> str:
        value = value.rstrip()
        if len(value) >= limit:
            value = value[: limit - 1].rstrip()
        return value + "…"

    for sep in ("。", "！", "？", "；", "\n"):
        index = window.rfind(sep, min_keep - 1)
        if index != -1:
            return with_ellipsis(window[: index + 1])
    for sep in ("，", ","):
        index = window.rfind(sep, min_keep - 1)
        if index != -1:
            return with_ellipsis(window[:index])
    return with_ellipsis(window)


def compose_generation_prompt(
    *,
    base: str,
    kind: str = "image",
    style: str | None = None,
    aspect_ratio: str = "9:16",
    assets: Sequence[dict] | None = None,
    negative: str | None = None,
    continuity: str | None = None,
    extra: str | None = None,
    with_quality: bool = True,
    subjects: Sequence[dict] | None = None,
    props: Sequence[dict] | None = None,
    timeline: Sequence[dict] | None = None,
    duration: float | None = None,
    start_state: dict | None = None,
    end_state: dict | None = None,
    directives: dict | None = None,
    references: Sequence[dict] | None = None,
    max_chars: int | None = None,
    empty_shot: bool = False,
    with_breakdown: bool = False,
    lean: bool = True,
) -> str | tuple[str, list[dict]]:
    """把一段基础提示词补全为完整生成指令。

    - ``base``：主体画面 / 动作描述（用户或分镜里已有的提示词）
    - ``style``：项目整体风格，作为风格收束追加
    - ``subjects`` / ``props``：本镜头人物绑定与道具归属（谁拿什么）
    - ``references``：实际随请求发送的参考图清单，生成编号与角色的对应说明
    - ``directives``：分镜里的景别 / 机位 / 运镜 / 氛围 / 构图
    - ``timeline``：秒级动作时间轴（图片取第一拍定格，视频按秒推进）
    - ``start_state`` / ``end_state``：首尾状态，供相邻镜头承接
    - ``assets``：关联资产列表（dict：kind / name / prompt / description）
    - ``continuity``：前后镜头衔接说明
    - ``extra``：额外的本张补充要求（如「起幅 / 中段 / 落幅」的阶段说明）
    - ``max_chars``：厂商提示词长度硬上限；超长时按优先级精简（见下方注释）
    """
    tagged: list[tuple[str, str]] = []

    def add(tag: str, text: str | None) -> None:
        if text and text.strip():
            tagged.append((tag, text.strip()))

    # 正文是模型注意力最集中的一段，但它超过 300 字后后半段基本被忽略；
    # 先按句边界收在 300 字内，避免后面的硬约束层被预算裁剪整块丢掉。
    if lean and base and len(base) > 300:
        base = _truncate_at_boundary(base, 300)
    add("base", base)
    if style and style.strip():
        add("style", f"【风格收束】{style.strip()}")
    # 视频的动作由时间轴逐拍负责，人物卡不再重复 action，避免同一句话占两份预算。
    add(
        "subjects",
        build_subject_block(
            subjects,
            props,
            empty_shot=empty_shot,
            compact=bool(timeline),
            lean=lean,
            # 时间轴已经逐拍写了动作/神态/视线，主体卡只留位置与道具归属
            skip_motion=lean and bool(timeline),
        ),
    )
    add("legend", build_reference_legend(references, compact=not lean, lean=lean))
    # 场景锚点先于镜头语言与动作：先交代「在哪、什么光」，再说「怎么拍、做什么」。
    # 静帧只需要起始状态；结束状态会给这张图引入尚未发生的信息。
    state_end = end_state if kind == "video" or not lean else None
    add(
        "state",
        build_state_block(
            start_state, state_end, compact=(kind == "video" or lean), lean=lean
        ),
    )
    directive_text = build_directives_block(directives, compact=(kind == "video" or lean))
    if lean and directive_text:
        directive_text = _truncate_at_boundary(directive_text, 48)
    add("directives", directive_text)
    # 正文自己就带秒级拍点时，结构化时间轴是同一批动作的第二份拷贝，跳过以免挤掉正文。
    base_has_beats = kind == "video" and bool(_BEAT_PATTERN.search(base))
    if not base_has_beats:
        add("timeline", build_timeline_block(timeline, kind=kind, duration=duration))
    add("extra", extra)
    # 已随请求发送角色/道具参考图时，外观由图片锁定，再写一大段文字外观是纯重复
    if not (lean and references):
        add("assets", build_asset_block(assets, compact=lean))
    if with_quality:
        add(
            "quality",
            build_quality_block(
                kind=kind,
                aspect_ratio=aspect_ratio,
                compact=(kind == "video"),
                empty_shot=empty_shot,
                lean=lean,
            ),
        )
    # 静帧的衔接信息已由起始状态与关键帧链承担，再塞一段相邻镜头摘要纯属重复
    if kind == "video" or not lean:
        add(
            "continuity",
            _truncate_at_boundary(continuity, 80) if (lean and continuity) else continuity,
        )
    else:
        add("continuity", None)
    add("count_check", build_reference_count_check(references, subjects))
    add(
        "negative",
        build_negative_block(negative, kind=kind, compact=(kind == "video" or lean), lean=lean),
    )

    if max_chars is not None:
        dropped_tags: list[str] = []

        def total_chars() -> int:
            return len("\n\n".join(text for _, text in tagged))

        def drop_block(tag_name: str) -> None:
            nonlocal tagged
            if any(t == tag_name for t, _ in tagged) and tag_name not in dropped_tags:
                dropped_tags.append(tag_name)
            tagged = [(t, text) for t, text in tagged if t != tag_name]

        def replace_block(tag_name: str, new_text: str) -> None:
            nonlocal tagged
            tagged = [(t, new_text if t == tag_name else text) for t, text in tagged]

        # 视频块已默认精简，先丢边际价值最低的整块：首帧已经锁住外貌与场景。
        # 图片不整块丢层，一律先压缩：extra 里的「起幅/中段/落幅」是区分多张关键帧的依据，
        # 资产外观是人物一致性的依据，丢掉就会生成三张一模一样的图或换个人。
        drop_order = ("assets", "extra") if kind == "video" else ()
        for tag in drop_order:
            if total_chars() <= max_chars:
                break
            drop_block(tag)

        # 每张图都一模一样的样板层先压：图例、画质、负面词。
        # 只删解释性文字，编号对应、形体底线、禁文字这些硬约束全部保留。
        if total_chars() > max_chars:
            compact_legend = build_reference_legend(references, compact=True, lean=lean)
            if compact_legend:
                replace_block("legend", compact_legend)
        if total_chars() > max_chars:
            replace_block(
                "quality",
                build_quality_block(
                    kind=kind,
                    aspect_ratio=aspect_ratio,
                    compact=True,
                    empty_shot=empty_shot,
                    lean=lean,
                ),
            )
        if total_chars() > max_chars:
            compact_negative = build_negative_block(negative, kind=kind, compact=True)
            if compact_negative:
                replace_block("negative", compact_negative)
            else:
                drop_block("negative")
        if total_chars() > max_chars:
            compact_assets = build_asset_block(assets, compact=True)
            if compact_assets:
                replace_block("assets", compact_assets)
        if kind == "image" and total_chars() > max_chars:
            # 静帧只定格在起始状态；结束状态与「下一镜承接」对这张图没有意义
            compact_state = build_state_block(start_state, None, compact=True, lean=lean)
            if compact_state:
                replace_block("state", compact_state)
        if kind == "video" and total_chars() > max_chars:
            # 视频保留【场景锚点】，只去掉与衔接块重复的「连续性」总述：
            # 丢掉锚点会让模型中途换房间，比少一句衔接说明严重得多
            compact_state = build_state_block(start_state, end_state, compact=True, lean=lean)
            if compact_state:
                replace_block("state", compact_state)
        if kind == "image" and total_chars() > max_chars and not lean:
            # 正文已经按第一个动作拍点定格，时间轴块是同一批动作的第二份拷贝
            drop_block("timeline")
        if kind == "video" and total_chars() > max_chars:
            replace_block("continuity", _VIDEO_CONTINUITY_COMPACT)
        if total_chars() > max_chars:
            # 优先压缩非正文块：正文承载全部动作语义，一旦被切，模型只能自行脑补，
            # 表现就是画面跳变、动作没有来由。先削最长的约束块，削到没意义了整块丢弃。
            # 镜头语言、主体绑定与时间轴属于硬约束，只允许被最后压缩，绝不整块丢弃——
            # 正文写得越细，字数越容易超限；此时如果先把 subjects 丢掉，
            # 「谁在画面里、谁拿什么、几个人」就没人管了，人物一致性会当场崩掉。
            protected = {
                "base",
                "directives",
                "subjects",
                "timeline",
                "negative",
                "legend",
                "count_check",
            }
            if lean:
                # 精简档的空间留给「在哪、谁、做什么」；场景锚点一旦被丢，画面会换房间
                protected.add("state")
            while total_chars() > max_chars:
                rest = [
                    (i, text)
                    for i, (tag_name, text) in enumerate(tagged)
                    if tag_name not in protected
                ]
                if not rest:
                    break
                index, text = max(rest, key=lambda item: len(item[1]))
                target = len(text) - (total_chars() - max_chars)
                if target < 40:
                    drop_block(tagged[index][0])
                    continue
                tagged[index] = (tagged[index][0], _truncate_at_boundary(text, target))
        if total_chars() > max_chars:
            # 极端兜底：非正文块已无可压缩，才压缩镜头语言，最后才动正文（按句边界截）。
            for tag_name in ("directives", "legend", "negative", "count_check"):
                if total_chars() <= max_chars:
                    break
                rest = [(i, text) for i, (t, text) in enumerate(tagged) if t == tag_name]
                if not rest:
                    continue
                index, text = rest[0]
                target = len(text) - (total_chars() - max_chars)
                if target < 20:
                    drop_block(tag_name)
                    continue
                tagged[index] = (tagged[index][0], _truncate_at_boundary(text, target))
        if total_chars() > max_chars and tagged:
            # 最后兜底：正文本身超限才动它，按句边界截，避免出现 "3.0-6.…" 这种半句。
            overflow = total_chars() - max_chars
            tag, text = tagged[0]
            tagged[0] = (tag, _truncate_at_boundary(text, len(text) - overflow))
    else:
        dropped_tags = []

    prompt = "\n\n".join(text for _, text in tagged)
    if with_breakdown:
        blocks = [{"tag": tag, "chars": len(text)} for tag, text in tagged]
        # 被整块丢弃的层补成 0 字，前端才能明确告警「这一层没进最终提示词」
        blocks.extend({"tag": tag, "chars": 0} for tag in dropped_tags)
        return prompt, blocks
    return prompt



# ---------------------------------------------------------------- 单字段 AI 生成
SHOT_FIELD_SYSTEM = """你是一名竖屏短剧导演兼分镜师，负责为「单个镜头」补写画面描述或生成提示词。
提示词是分层编译的：环境、镜头语言、人物固定外貌、道具归属、秒级时间轴、风格、画质、
负面词、前后衔接都由系统按层注入。你只写「正文」这一层。
要求：
1. 只输出 JSON：{"value": "..."}，不要输出任何解释、Markdown 代码块标记或多余文字。
2. 严格按任务指定的字段类型与语言输出（画面描述与提示词一律用中文）。
3. 必须承接给定的前后镜头上下文，保持人物、服装、场景与色调一致。
   地点 / 时间 / 天气 / 光线这类静态事实不要重复；但环境中与动作发生互动的瞬态细节必须写，
   例如雨点砸在玻璃上炸开的水花、冷光在刀锋上划过的一线反光、脚步带起的粉笔灰。
4. 本镜头的提示词必须与其他镜头明显不同，不要写成雷同画面。
5. 正文要写足这一层独有的细节，宁可具体，不要空泛。每个镜头至少包含：
   ① 每个主体 3 个以上可见微细节（指节用力泛白、发丝被汗粘在额角、衣料受力的褶皱方向）；
   ② 每件道具 2 个物理细节（金属反光位置、杯壁凹陷、握着它的手指压痕）；
   ③ 至少 1 处明确的受光位置与阴影落点；
   ④ 至少 1 组前 / 中 / 后景的空间关系；
   ⑤ 动作的力度、速度或惯性（不是「他很紧张」，而是「指节抵得发白、肩膀绷紧到发抖」）。
   禁止只写抽象情绪判断，情绪必须落到可见的表情、肌肉、呼吸或小动作上。
6. image_prompt 描述第一个动作拍点（镜头开始时）的定格瞬间；video_prompt 写动作随时间的变化过程。
   若给了 action_timeline，video_prompt 仍要写足每个阶段的可见细节，只是不要重复「0.0-1.5s：…」
   这类时间戳；没有时间轴时才按秒写。
7. 必须使用 subjects 里的姓名，禁止用「他 / 她 / 两人」代替。不要照抄固定外貌、服装款式与站位
   （这些由主体层与资产层注入），但人物在本镜头里的瞬态变化必须写：被雨打湿的发梢、袖口的水渍、
   呼吸起伏、鞋底泥点、额头细汗。道具归属由道具层负责，正文只写道具当前的物理状态。
8. 中文文本里的引号一律使用中文引号「」或“”，禁止使用英文直引号（"），以免破坏 JSON 结构。"""

# 各字段的产出规范
_SHOT_FIELD_SPECS: dict[str, str] = {
    "description": (
        "中文「画面描述」：3-5 句话、180-260 字（280 字为硬上限），写清人物在做什么、"
        "神态与情绪如何变化，以及这一镜最关键的视觉瞬间。必须落到可见细节：手部动作与力度、"
        "面部微表情、衣料或发丝的瞬态变化、环境与动作的互动（雨、光、灰尘、影子）。"
        "信息密度优先，不要用同义词堆字数。"
        "不要照抄固定外貌、服装款式、地点 / 时间 / 天气 / 光线等静态事实，"
        "也不要写景别 / 机位 / 运镜、风格与画质词。不要出现镜头术语编号、字幕、水印。"
    ),
    "image_prompt": (
        "中文「关键帧（文生图）提示词」：完整一段，逗号分隔，200-350 字（不设更长的散文）。必须写足以下可见细节：\n"
        "① 动作瞬间：定格在第一个动作拍点「正在进行 / 尚未完成」的状态，不写完成态结果；\n"
        "② 神态、视线与面部微表情（眉、眼、下颌、嘴唇、呼吸的可见变化）；\n"
        "③ 手部与道具的接触方式：手指位置、指节受力、压痕、道具的物理状态；\n"
        "④ 材质与质感的局部细节：布料褶皱走向、金属反光位置、皮肤纹理、汗或水的痕迹；\n"
        "⑤ 至少 1 处明确的光线落点与阴影，以及 1 组前 / 中 / 后景的空间关系；\n"
        "⑥ 环境中与动作互动的瞬态细节（雨点砸在玻璃上的水花、脚步带起的灰尘），"
        "但不要重复地点 / 时间 / 天气这类静态事实。\n"
        "禁止只写「神情紧张」「气氛压抑」这类抽象判断；不要写固定外貌与服装款式、"
        "景别 / 机位 / 运镜、风格关键词、画质词、负面词——这些由系统分层注入。\n"
        "必须与相邻镜头画面明显不同，且不得与其他镜头雷同。"
    ),
    "video_prompt": (
        "中文「图生视频提示词」：完整一段，200-320 字（340 字为硬上限），写动作随时间如何变化："
        "起始姿态 → 过程中的关键变化 → 收尾状态。每个阶段都要有可见细节："
        "动作的力度与速度、手部轨迹、衣料 / 头发 / 道具的惯性反应、表情的逐步变化、"
        "环境对动作的即时反馈（水花、灰尘、光影晃动）。\n"
        "若给了秒级时间轴，正文写足各阶段的细节，但不要重复「0.0-1.5s：…」这类时间戳"
        "（时间轴由系统注入）；没有时间轴时才按秒写。\n"
        "不要照抄固定外貌与服装、地点 / 时间 / 天气等静态事实，也不要写镜头运动与景别、"
        "画质词、负面词——这些由系统分层注入。"
    ),
    # ---- 镜头语言分层：每层单独生成，避免改一个运镜就重写整段提示词 ----
    "shot_size": (
        "中文「景别」：只输出一个词组，如「中近景」「全景」「特写」。"
        "按剧情张力选择：交代环境用全景 / 中景，情绪与眼神用近景 / 特写。不要解释。"
    ),
    "camera_angle": (
        "中文「机位与角度」：只输出一个词组，如「平视」「略俯拍」「低角度仰拍」「过肩」。"
        "用于强化人物力量关系，不要写句子。"
    ),
    "camera_movement": (
        "中文「运镜」：只输出一个词组，如「固定机位」「缓慢推近」「横向摇移」「跟随」。"
        "不要写速度数值以外的解释。"
    ),
    "composition": (
        "中文「构图与空间锚点」：一句话，说明主体在画面中的位置、前景 / 背景层次与轴线关系，"
        "如「林越居左前景，陈默居右中景，两人之间留出桌面纵深」。不超过 40 字。"
    ),
    "atmosphere": (
        "中文「氛围」：一句话描述环境氛围、光位与冷暖，如「夜间教室，顶部日光灯冷白，"
        "窗外青黑色雾气透入」。不超过 40 字。"
    ),
    "mood_tags": (
        "中文「情绪标签」：3-4 个词，用「、」分隔，如「警觉、压抑、不安」。只输出标签，不要句子。"
    ),
}

# 这些是镜头语言分层字段，值是一句短语而非整段提示词，生成后写回 shots.directives
DIRECTIVE_FIELD_KEYS: tuple[str, ...] = (
    "shot_size",
    "camera_angle",
    "camera_movement",
    "composition",
    "atmosphere",
    "mood_tags",
)

SHOT_FIELD_KEYS: tuple[str, ...] = tuple(_SHOT_FIELD_SPECS.keys())


def build_shot_field_prompt(
    *,
    field: str,
    title: str | None,
    description: str | None,
    dialogue: str | None,
    style: str | None,
    aspect_ratio: str,
    prev_summary: str | None = None,
    next_summary: str | None = None,
    hint: str | None = None,
    current_value: str | None = None,
    script: str | None = None,
    characters: Sequence[dict] | None = None,
    subjects: Sequence[dict] | None = None,
    props: Sequence[dict] | None = None,
    timeline: Sequence[dict] | None = None,
    start_state: dict | None = None,
    end_state: dict | None = None,
    directives: dict | None = None,
    assets: Sequence[dict] | None = None,
    duration: float | None = None,
) -> str:
    """为分镜的单个字段（画面描述 / 关键帧提示词 / 视频提示词）生成内容。"""
    spec = _SHOT_FIELD_SPECS[field]
    orientation = "竖屏" if aspect_ratio == "9:16" else "横屏" if aspect_ratio == "16:9" else "方形"
    lines = [
        f"【任务】{spec}",
        f"【画幅】{orientation}（{aspect_ratio}）",
        f"【整体风格】{style or '未指定，请给出统一且适合短剧的写实风格'}",
    ]
    if title:
        lines.append(f"【镜头标题】{title}")
    has_subjects = any(
        isinstance(item, dict) and item.get("name") for item in (subjects or [])
    )
    if field in ("description", "image_prompt", "video_prompt"):
        # 分层去重：只列「本镜头确实已经填了」的层，避免正文把这些内容再写一遍。
        owned: list[str] = []
        if (start_state or end_state) and has_subjects:
            # 纯空镜没有人物可写，环境本身就是画面正文，不能禁（否则正文会空）。
            owned.append("环境（地点 / 时间 / 天气 / 光线 / 站位）")
        if any(isinstance(directives, dict) and directives.get(key) for key in DIRECTIVE_FIELD_KEYS):
            owned.append("镜头语言（景别 / 机位 / 运镜 / 构图 / 氛围 / 情绪）")
        if has_subjects:
            has_locked_look = any(
                isinstance(item, dict) and (item.get("appearance") or item.get("costume") or item.get("asset_id"))
                for item in subjects or []
            )
            if has_locked_look:
                owned.append("人物固定外貌与服装")
        if props:
            owned.append("道具归属")
        if timeline:
            owned.append("秒级动作时间轴")
        if assets:
            owned.append("资产外观锁定描述")
        owned.append("整体风格、画质要求、负面提示、前后镜头衔接")
        lines.append(
            "【分层去重】以下内容已由其它层统一注入，正文不要照抄原句："
            + "；".join(owned)
            + "。但「瞬态细节」必须写足：这几个层没有覆盖的微表情、手部受力、"
            "衣料 / 发丝 / 皮肤的即时变化、光线落点与阴影、环境对动作的反馈（水花 / 灰尘 / 影子晃动）。"
            "目标是让画面具体到可以直接拍，而不是只写抽象情绪。"
        )
    if description:
        lines.append(f"【当前画面描述】{description}")
    if dialogue:
        lines.append(f"【对白 / 旁白】{dialogue}")
    if current_value:
        lines.append(f"【当前字段内容（在此基础上改写或补全，不要偏离事实）】{current_value}")
    if script:
        lines.append(f"【本集剧本上下文（用于理解前后剧情）】\n{script[:5000]}")
    if characters:
        char_lines = []
        for item in characters:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or ""
            look = "；".join(
                str(item.get(key)) for key in ("appearance", "costume") if item.get(key)
            )
            char_lines.append(f"- {name}：{look}" if look else f"- {name}")
        if char_lines:
            lines.append(
                "【角色档案（用于识别身份；外貌与服装由系统分层注入，正文不要重复）】\n"
                + "\n".join(char_lines)
            )

    subject_block = build_subject_block(subjects, props)
    if subject_block:
        lines.append(subject_block)
    directives_block = build_directives_block(directives)
    if directives_block:
        lines.append(directives_block)
    timeline_block = build_timeline_block(
        timeline, kind="image" if field == "image_prompt" else "video", duration=duration
    )
    if timeline_block:
        lines.append(timeline_block)
    state_block = build_state_block(start_state, end_state)
    if state_block:
        if has_subjects and field in ("description", "image_prompt", "video_prompt"):
            state_block += "\n（以上为环境事实，仅供理解与保持一致性，正文不要重复）"
        lines.append(state_block)
    asset_block = build_asset_block(assets)
    if asset_block:
        lines.append(asset_block)
    if hint:
        lines.append(f"【本张补充要求】{hint}")
    block = build_continuity_block(prev_summary=prev_summary, next_summary=next_summary)
    if block:
        lines.append(block)
    lines.append('请只输出 JSON：{"value": "..."}')
    return "\n\n".join(lines)


# ---------------------------------------------------------------- 新建项目建议
PROJECT_SUGGEST_SYSTEM = """你是一名短剧制片人兼创意总监。
用户会给出一个粗糙的主题想法，你要把它补全成一个可直接开拍的项目设定。
要求：
1. 只输出 JSON，不要输出任何解释、Markdown 代码块标记或多余文字。
2. 标题要有画面感与钩子，中文 12 字以内。
3. 简介 120-180 字：写清主角身份与处境、核心物件或异常规则、最大冲突、以及留人的钩子；
   用 2-3 句话，不要只写一句空泛的题材概括。
4. 若灵感提到具体作品、角色或世界观，必须沿用原作的主角身份与核心设定，不得替换成
   无关职业或另起一个新主角；不确定原作细节时，只围绕灵感里明确写到的元素展开。
5. visual_style 只能从给定选项里选一个最贴合的；video_style 只能从给定选项里选一个。
6. style 给出可复用的画面风格关键词（如「赛博朋克、霓虹冷调、写实电影感」），逗号分隔。
7. tags 给出 3-5 个题材标签。
8. 中文文本里的引号一律使用中文引号「」或“”，禁止使用英文直引号（"），以免破坏 JSON 结构。"""

_PROJECT_SUGGEST_SCHEMA = """{
  "title": "项目标题（中文，12 字以内）",
  "description": "2-3 句话的简介（中文，120-180 字）",
  "visual_style": "画面质感，只能选：现实 / 日式动漫 / 国风水墨 / 油画质感 / 赛博朋克 / 3D 动画 / 复古胶片 / 卡通",
  "video_style": "题材赛道，只能选：真人都市 / 古装仙侠 / 现代校园 / 悬疑惊悚 / 甜宠爱情 / 科幻未来 / 历史正剧 / 武侠江湖",
  "style": "画面风格关键词（中文，逗号分隔）",
  "aspect_ratio": "9:16 或 16:9 或 1:1 或 4:3",
  "tags": ["题材标签1", "题材标签2", "题材标签3"]
}"""


def build_project_suggest_prompt(*, idea: str, aspect_ratio: str) -> str:
    """把用户输入的粗略想法补全为项目设定。"""
    orientation = "竖屏" if aspect_ratio == "9:16" else "横屏" if aspect_ratio == "16:9" else "方形"
    return f"""下面是我关于一部短剧的粗略想法：

{idea.strip()}

【默认画幅】{orientation}（{aspect_ratio}）
请把它补全成可直接开拍的项目设定，并严格按以下 JSON 结构输出（字段含义见注释，实际输出不要包含注释）：
{_PROJECT_SUGGEST_SCHEMA}"""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _escape_inner_quotes(text: str) -> str:
    """把字符串值内部未转义的裸引号转义为 \\"。

    模型长文本输出（尤其中文剧本描述）常出现 ``"记忆—分期付"`` 这类裸引号，
    会破坏 JSON。判定规则：字符串内的引号若其后（跳过空白）不是 ``,`` ``}`` ``]`` ``:``，
    则视为内容引号而非结束引号。
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            continue
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in ",}]:[":
                out.append(ch)
                in_string = False
            else:
                out.append('\\"')
            continue
        out.append(ch)
    return "".join(out)


def _json_variants(text: str) -> list[str]:
    """按容错强度递增生成候选串：原文 → 截取花括号 → 修引号 → 去尾逗号。"""
    variants = [text]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return variants
    inner = text[start : end + 1]
    variants.append(inner)
    repaired = _escape_inner_quotes(inner)
    variants.append(repaired)
    trimmed = _TRAILING_COMMA_RE.sub(r"\1", repaired)
    variants.append(trimmed)
    return variants


def _close_open_structures(text: str) -> str:
    """补全未闭合的字符串与括号，用于救回被截断的模型输出。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    result = text.rstrip()
    if in_string:
        result += '"'
    result = result.rstrip(",")
    for ch in reversed(stack):
        result += "}" if ch == "{" else "]"
    return result


def _salvage_json(text: str) -> dict:
    """按解析器报错位置逐个修补漏逗号 / 尾部截断，尽量保住已经完整的前半部分。

    一次分镜拆解要跑两分多钟，不能因为模型漏写一个逗号就整轮作废。
    """
    candidate = text
    for _ in range(60):
        try:
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError as exc:
            msg, pos = exc.msg, exc.pos
            if msg.startswith("Expecting ',' delimiter"):
                candidate = candidate[:pos] + "," + candidate[pos:]
                continue
            if msg.startswith("Extra data"):
                candidate = _close_open_structures(candidate[:pos])
                continue
            cut = candidate[:pos]
            boundary = max(cut.rfind("},"), cut.rfind("}]"), cut.rfind('",'))
            if boundary <= 0:
                raise
            candidate = _close_open_structures(cut[: boundary + 1])
    raise ValueError("JSON 修补后仍无法解析")


def extract_json(text: str) -> dict:
    """从模型输出中提取 JSON，兼容代码块包裹、前后缀噪声与常见格式瑕疵。"""
    if not text:
        raise ValueError("模型返回为空")
    candidate = text.strip()
    match = _FENCE_RE.search(candidate)
    if match:
        candidate = match.group(1).strip()

    variants = _json_variants(candidate)
    last_error: json.JSONDecodeError | None = None
    for variant in variants:
        try:
            # strict=False 允许字符串内出现裸换行等控制字符
            return json.loads(variant, strict=False)
        except json.JSONDecodeError as exc:
            last_error = exc
    for variant in variants:
        try:
            data = _salvage_json(variant)
        except ValueError:
            continue
        logger.warning("模型输出的 JSON 有语法瑕疵，已自动修补解析（原文长度 %s）", len(variant))
        return data
    raise ValueError(f"模型返回的 JSON 无法解析：{last_error}") from last_error
