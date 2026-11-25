"""XSC（小升初）领域词表与暗语注入。

为什么需要这个：
  ASR 模型（无论 whisper 还是 Gemini）没有这场直播的领域知识，
  会把"省实"听成"省市"、"金广附"听成"金广复"、"指标到校"听成"指标的道校"。
  把已知的学校名/暗语/术语作为上下文喂给模型，能显著降低这类错误。

数据来源：
  - D:\\File\\01_Vibe_Coding\\09-WeChat_analysis\\...\\01_XSC暗号超全解读_各学校代码.md
  - D:\\File\\01_Vibe_Coding\\09-WeChat_analysis\\...\\02_最新XSC暗号解读.md
  以及实测直播中出现的真实错误对。

用法：
  1) 作为 prompt 的一部分注入（Gemini）—— 让模型知道这些词的存在
  2) 作为事后纠错词表（DeepSeek）—— 按映射表统一替换
  3) 作为 whisper 的 initial_prompt（本地引擎）—— 提供领域偏置
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# ---------------------------------------------------------------- 学校名（正式名）
SCHOOLS = [
    "省实", "广东实验中学", "华附", "华南师范大学附属中学", "二中", "广州市第二中学",
    "执信", "执信中学", "广雅", "广雅中学", "铁一", "铁一中学", "六中", "广州市第六中学",
    "广大附", "广附", "金广附", "金山广附", "黄埔广附", "白云广附",
    "天省", "天河省实", "天河外国语", "广外", "广州外国语",
    "培英", "云城培英", "白云培英", "真光", "培正", "南武", "七中", "十六中", "八十六中",
    "名德实验", "中大附", "中大附中", "育才实验", "育实", "二中苏元", "二苏",
    "二中应元", "应元", "八一实验", "华侨外国语", "西关外国语", "荔湾广雅",
    "白云华赋", "华赋", "黄埔铁英", "铁英", "天河华附", "华附番禺", "番禺华附",
    "南沙广附", "增城广附", "花都广雅", "从化中学", "仲元", "禺山", "象贤",
    "南海中学", "协和", "三中", "四中", "五中", "九十七中", "南武实验",
    # 实测直播中出现、但通用暗语表未收录的简称（2026-11-16 实录补充）
    "白省", "白云省实", "白雅", "白云广雅", "龙涛", "培文",
    "学而思", "南山班", "奥班", "创新班", "重点班",
]

# ---------------------------------------------------------------- 暗语 → 真实含义
# 只收录资料中明确给出对应关系的，避免臆造
CODES: dict[str, str] = {
    # 拼音首字母类
    "SS": "省实", "HF": "华附", "EZ": "二中", "GF": "广附", "ZX": "执信",
    "GY": "广雅", "TY": "铁一", "JGF": "金广附", "TX": "天省", "HPGF": "黄埔广附",
    "MDSY": "名德实验", "16Z": "十六中", "86Z": "八十六中", "NS": "南山",
    "2J": "二基", "YY": "二中应元", "ZDF": "中大附", "LG": "六改",
    # 谐音/联想类
    "景观房": "金广附", "香鸡腿": "铁一", "石榴姐妹": "十六中",
    # 注意：小金龙 = 番禺祈福新邨（QFXC/XL），**不是金广附**。
    # 金广附是位于白云区金沙洲的广大附中实验学校。2026-11-16 King 订正。
    "小金龙": "祈福新邨", "QFXC": "祈福新邨", "XL": "祈福新邨",
    "清湾": "清湾", "QW": "清湾", "QHFZ": "清湾",
    "龙哥": "六改", "八姨": "八一实验", "二叔": "二中苏元",
    "银元": "二中应元", "银店": "二中应元", "金店": "中大附", "周大福": "中大附",
    "浴室": "育才实验", "育实": "育才实验",
    # 食物类
    "鸡腿": "铁一", "白切鸡": "白云铁一", "烧鸡": "白云华赋",
    "芝麻鸡": "六中", "黄焖鸡": "黄埔铁英", "烧鸭": "白云华赋",
    # 地点类
    "区庄": "执信", "眼科医院": "执信", "农讲所": "省实", "流花湖": "广雅",
    "天河客运站": "天省", "二沙岛": "执信", "长洲岛": "黄埔广附",
    "富颐华庭": "黄埔广附", "张之洞": "广雅", "张府": "广雅",
    # 实测直播（2026-11-16）中出现的地域简称
    "白省": "白云省实", "白雅": "白云广雅",
}

# ---------------------------------------------------------------- XSC 常用术语
TERMS = [
    "小升初", "XSC", "中考", "指标到校", "名额分配", "直升", "派位", "摇号",
    "密考", "MK", "人机", "FBC", "分班考", "信息营", "坑班", "开放日",
    "一线学校", "二线学校", "准一线", "民办", "公办", "公参民", "转公",
    "加工能力", "出口成绩", "均分", "普高率", "示范性高中", "省一级",
    "杯赛", "华杯", "希望杯", "迎春杯", "走美杯", "AMC", "世奥赛",
    "冬令营", "夏令营", "研学", "特长", "自主招生", "签约", "自招",
    "学位房", "地段生", "积分入学", "统筹", "对口直升", "九年一贯",
    "班型", "重点班", "次重点", "平行班", "实验班", "创新班",
]

# ---------------------------------------------------------------- ASR 实测错误 → 正确
# 这些是本项目用真实直播音频测出来的，不是猜的
CORRECTIONS: dict[str, str] = {
    "省市用品": "省实",
    "盛世用平": "省实",
    "省实用品": "省实",
    "陈氏用品": "省实",
    "云城培营": "云城培英",
    "云山赔英": "云城培英",
    "云城培英": "云城培英",
    "云省培英": "云城培英",
    "云神培林": "云城培英",
    "金广复": "金广附",
    "金广附和": "金广附和",
    "指标的道校": "指标到校",
    "指标道校": "指标到校",
    "名額指標": "名额指标",
    "明半": "民办",
    "公半": "公办",
    "民半": "民办",
    "它行驾": "他寒假",
    "东丽银": "冬令营",
    "东丽宁": "冬令营",
    "胖道": "判断",
    "抗礼": "靠前",
    "抗旅": "靠前",
    "MKY区": "MK外区",
    "MK外聚": "MK外区",
    "假假你": "假设你",
    "讲讲你": "假设你",
    "想想你": "假设你",
    "超临冬": "朝翎东",
    "茶号会": "茶话会",
    "达小时": "大学时",
    "730家": "730+",
    "670家": "670+",
    "准营线": "准一线",
    "二线的准营线": "二线的准一线",
    # 同一简称的多种写法归一（实测同一场直播里"白六"和"百六"混用，会破坏检索）
    "百六": "白六",
    "白六中": "白六",
    "华初中学": "华初",
    "东江外国语学校": "东江外国语",
}

# 单独出现的"家"需谨慎替换（"大家""家长"都是正常词），
# 所以只在数字后紧跟"家"时替换
_RE_NUM_JIA = re.compile(r"(\d{3})\s*家")

# 繁→简 常用字兜底表（覆盖实测出现的高频字，opencc 缺失时启用）
_TC2SC = str.maketrans(
    "個們這來說時會後裡麼為與對開關實現點線樣區問間還進過發現當從學經濟這樣沒電話"
    "讓給帶東車長門見覺聽講讀寫買賣價錢銀藝術動員員國圖書畫筆記億萬歲歷經歷",
    "个们这来说时会后里么为与对开关实现点线样区问间还进过发现当从学经济这样没电话"
    "让给带东车长门见觉听讲读写买卖价钱银艺术动员员国图书画笔记亿万岁历经历",
)
_OPENCC_T2S = None
_OPENCC_TRIED = False


def to_simplified(text: str) -> str:
    """繁转简。优先用 opencc（缓存实例），缺失时用内置映射兜底。"""
    if not text:
        return text
    global _OPENCC_T2S, _OPENCC_TRIED
    if not _OPENCC_TRIED:
        _OPENCC_TRIED = True
        try:
            from opencc import OpenCC

            _OPENCC_T2S = OpenCC("t2s")
        except Exception:
            _OPENCC_T2S = None
            # 内置兜底表只有几十个常用字，大段繁体不会被转换。
            # 静默降级会让人误以为整份产物已是简体，所以必须提示一次。
            print("[lexicon][warn] 未安装 opencc，繁转简仅使用内置常用字兜底表"
                  "（覆盖有限，建议 pip install opencc-python-reimplemented）")
    if _OPENCC_T2S is not None:
        return _OPENCC_T2S.convert(text)
    return text.translate(_TC2SC)


DEFAULT_PATH = Path(__file__).resolve().parent.parent / "lexicon" / "xsc.json"


def _num_jia_fix(text: str) -> str:
    """把"730家"改成"730+"，但放过"大家/家长"这类正常用法。"""
    return _RE_NUM_JIA.sub(r"\1+", text)


def build_prompt_hint(max_terms: int = 120) -> str:
    """生成注入到 ASR prompt 的领域提示。

    注意控制长度：prompt 太长会稀释模型注意力，实测 100-150 个词效果最好。
    也绝不能写成"以下是……"这种叙述句 —— whisper 会把它当上文复读。
    """
    schools = "、".join(SCHOOLS[:40])
    terms = "、".join(TERMS[: max_terms - 40])
    return (
        f"本音频为广州小升初(XSC)升学规划直播。"
        f"专有名词参考：{schools}。"
        f"术语参考：{terms}。"
        f"上述仅为识别参考，不要朗读或复述这些词。"
    )


def correct_text(text: str) -> str:
    """按实测错误表做纠正。长词优先，避免"省实"被"省"截断。

    数字保护：中文数字/阿拉伯数字紧邻时不做替换。
    实测坑：CORRECTIONS 里有 "百六→白六"（白六中简称），
    若不保护，"一百六十个人"会被改成"一白六十个人"、"三百六十五天"
    变成"三白六十五天"。这类文本在讲分数、人数、天数时很常见。
    """
    if not text:
        return text
    out = text
    for wrong in sorted(CORRECTIONS, key=len, reverse=True):
        right = CORRECTIONS[wrong]
        if wrong == right or wrong not in out:
            continue
        out = _replace_guarded(out, wrong, right)
    return _num_jia_fix(out)


# 中文数字与阿拉伯数字。仅当替换词**首字符或尾字符本身是数字**时才做邻接检查，
# 避免 "白六"(尾字"六"是数字) 被无谓跳过。
_DIGIT_CHARS = "0123456789零一二三四五六七八九十百千万亿两"


def _replace_guarded(text: str, wrong: str, right: str) -> str:
    """替换 wrong→right，跳过夹在数字中间的匹配。

    判据只看「替换词的两端」：
      - 若 wrong 首字符是数字，且左侧紧邻也是数字 → 疑似数字串，跳过
        （"一百六十" 里 "百六"，左邻 "一" → 跳过）
      - 若 wrong 尾字符是数字，且右侧紧邻也是数字 → 跳过
        （"六百七" 里 "百六"? 不成立，但 "六十" 右邻 "个" 是量词 → 不跳过）
    这样 "百六的课改班"（右邻"的"）能正常替换成 "白六的课改班"。
    """
    head_is_digit = wrong[0] in _DIGIT_CHARS
    tail_is_digit = wrong[-1] in _DIGIT_CHARS
    if not head_is_digit and not tail_is_digit:
        return text.replace(wrong, right)

    out = []
    i = 0
    n = len(wrong)
    hit = False
    while True:
        j = text.find(wrong, i)
        if j < 0:
            out.append(text[i:])
            break
        left = text[j - 1] if j > 0 else ""
        right_ch = text[j + n] if j + n < len(text) else ""
        # 必须显式判空："" in "0123…" 恒为 True（空串是任何字符串的子串），
        # 漏了这一步会把所有句首/句尾的词都误判成"紧邻数字"而永不替换。
        skip = ((head_is_digit and left and left in _DIGIT_CHARS)
                or (tail_is_digit and right_ch and right_ch in _DIGIT_CHARS))
        if skip:
            out.append(text[i:j + n])
        else:
            out.append(text[i:j])
            out.append(right)
            hit = True
        i = j + n
    return "".join(out) if hit else text


# 这些代号/proper name 已是通用叫法，展开成括号纯属噪音，反而影响可读性
_NO_EXPAND = {
    "金广附", "省实", "华附", "二中", "广雅", "执信", "铁一", "六中",
    "广大附", "培英", "培正", "真光", "南武", "五中", "七中", "四中", "三中", "一中",
    "白云省实", "白云广雅", "白云六中", "白云铁一", "白云华赋",
    "天河省实", "荔湾省实", "荔湾广雅", "西关广雅", "花都广雅",
    "黄埔广附", "花都广附", "增城广附", "南沙广附",
    "二中苏元", "二中应元", "二中天元", "二中开元", "二中会元",
    "十六中", "十六中实验", "八十六中", "九十七中", "十三中", "一一三中学",
    "华侨外国语", "西关外国语", "天河外国语", "江南外国语", "海珠实验",
    "中大附中", "育才实验", "育才中学", "八一实验", "名德实验",
    "祈福新邨", "祈福英校", "番禺华附", "番禺铁一", "亚运城铁一",
    "华附奥校", "广州外国语", "广州中学", "广州实验中学", "天河中学",
    "清湾", "黄埔华实", "黄埔军校", "黄埔铁英", "培文外国语",
    "东江外国语", "荔广实验", "云城培英", "白云华赋", "华美实验",
}


# 这些短代码容易被更长的词"吃掉"一部分，需检查左侧是否构成更长词
_SUFFIX_GUARDED = {"金奥", "小奥", "大奥", "奥班", "龙哥", "白省", "白雅", "白六"}


def expand_codes(text: str, keep_original: bool = True) -> str:
    """把暗语展开成正式校名，便于后续 LLM 理解和检索。

    keep_original=True 时输出 `暗语（正式名）`，保留原文可追溯。

    实现要点（2026-11-16 实测踩坑后重写）：
    不能用「逐词全局 replace」，实测会产生三类错：
      1. 括号嵌套：code 的正式名里也含 code（如 清实→清远实验（清实））
         → 二次扫描会把括号里的又展开一遍，变成 `清实（清远实验（清实））`
      2. 长短覆盖：先替换 `小金` 会破坏 `小金龙`，
         变成 `小金（金广附）龙（金广附）`
      3. 重复展开：替换出来的正式名若含别的 code，又被匹配一次

    正确做法：**单次从左到右扫描**，在最左位置优先匹配最长的 code，
    替换后跳过整段（含括号），不再回头处理已展开内容。
    """
    if not text:
        return text

    # 按长度降序，保证"最左最长优先"。
    # 跳过"已经是通用叫法"的条目：如 金广附→金广实验学校、省实→广东实验中学，
    # 这些叫法本身就足够清楚，展开成括号只会增加噪音。
    codes = sorted((c for c in CODES if c not in _NO_EXPAND), key=len, reverse=True)
    n = len(text)
    out: list[str] = []
    i = 0
    while i < n:
        matched: str | None = None
        for code in codes:
            if not text.startswith(code, i):
                continue
            # 纯字母代码要求词边界，避免 "SS" 误伤其他英文
            if code.isascii():
                prev_ch = text[i - 1] if i > 0 else ""
                next_ch = text[i + len(code)] if i + len(code) < n else ""
                if prev_ch.isascii() and prev_ch.isalpha():
                    continue
                if next_ch.isascii() and next_ch.isalpha():
                    continue
            # 后缀冲突保护：`金奥` 不能吃掉 `小金奥班` 里的 `金奥`。
            # 判据：左侧紧邻字符与 code 拼起来，若能构成词表里某个词的
            # 前缀（如 "小"+"金奥" 是 "小金奥班" 的前缀），说明我们切在词中间。
            if code in _SUFFIX_GUARDED:
                prev_ch = text[i - 1] if i > 0 else ""
                if prev_ch:
                    head = prev_ch + code
                    if any(w.startswith(head) and len(w) > len(head)
                           for w in CODES):
                        continue
            matched = code
            break

        if matched is None:
            out.append(text[i])
            i += 1
            continue

        target = CODES[matched]
        # 正式名里若还含自身 code，去掉重复的括号注释，避免嵌套
        if matched in target:
            target = target.replace(f"（{matched}）", "").strip()

        # 幂等保护：源文本若已是 `code（正式名）` 形式，直接原样保留，
        # 不再追加一层。实测坑：转录稿反复过词表会变成
        # `小金龙（金广附）（金广附）`，句子越跑越脏。
        already = f"{matched}（{target}）"
        if text.startswith(already, i):
            out.append(already)
            i += len(already)
            continue

        out.append(f"{matched}（{target}）" if keep_original else target)
        i += len(matched)

    return "".join(out)


def _clean_map(d: dict) -> dict:
    """过滤掉分组标记与空值。

    词表里允许用 `"分组名": null` 或 `"_说明": ...` 做可读性分隔，
    但这些不是真实映射，必须剔除，否则会参与匹配、甚至在
    `sorted(CODES, key=len)` 时因 None 崩溃。
    """
    out = {}
    for k, v in (d or {}).items():
        if not isinstance(k, str):
            continue
        if k.startswith("_") or v is None:
            continue
        if not isinstance(v, str) or not v.strip():
            continue
        out[k] = v
    return out


def load(path: Path | None = None) -> dict:
    """加载外部词表（优先），与内置表合并。

    显式传入的 path 不存在时必须告警：静默回退内置表会让“已配置的外部词表”
    看起来生效、实际全是内置词。path=None 表示使用默认路径，默认不存在
    只回退不告警（纯本地安装可能就没有 lexicon/）。
    """
    explicit = path is not None
    p = path or DEFAULT_PATH
    data = {"schools": list(SCHOOLS), "terms": list(TERMS),
            "codes": dict(CODES), "corrections": dict(CORRECTIONS)}
    if p.exists():
        try:
            ext = json.loads(p.read_text(encoding="utf-8"))
            data["schools"] = list(dict.fromkeys(data["schools"] + ext.get("schools", [])))
            data["terms"] = list(dict.fromkeys(data["terms"] + ext.get("terms", [])))
            data["codes"].update(_clean_map(ext.get("codes", {})))
            data["corrections"].update(_clean_map(ext.get("corrections", {})))
            print(f"[lexicon] 已加载外部词表 {p.name}（+{len(ext.get('schools', []))} 校名）")
        except Exception as e:  # noqa: BLE001
            print(f"[lexicon][warn] 外部词表加载失败，用内置表: {e}")
    elif explicit:
        print(f"[lexicon][warn] 指定的词表文件不存在，回退内置表: {p}")
    return data


def apply_loaded(data: dict) -> None:
    """把 load() 的结果写回模块级表，供全局使用。"""
    global SCHOOLS, TERMS, CODES, CORRECTIONS
    SCHOOLS = data["schools"]
    TERMS = data["terms"]
    CODES = data["codes"]
    CORRECTIONS = data["corrections"]
