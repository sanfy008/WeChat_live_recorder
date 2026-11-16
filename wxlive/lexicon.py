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
    "小金龙": "金广附", "龙哥": "六改", "八姨": "八一实验", "二叔": "二中苏元",
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
    """按实测错误表做纠正。长词优先，避免"省实"被"省"截断。"""
    if not text:
        return text
    out = text
    for wrong in sorted(CORRECTIONS, key=len, reverse=True):
        right = CORRECTIONS[wrong]
        if wrong != right and wrong in out:
            out = out.replace(wrong, right)
    return _num_jia_fix(out)


def expand_codes(text: str, keep_original: bool = True) -> str:
    """把暗语展开成正式校名，便于后续 LLM 理解和检索。

    keep_original=True 时输出 `暗语（正式名）`，保留原文可追溯。
    """
    if not text:
        return text
    out = text
    for code in sorted(CODES, key=len, reverse=True):
        if code.isascii():
            # 纯字母代码要求词边界，避免 "SS" 误伤其他英文
            pattern = re.compile(rf"(?<![A-Za-z]){re.escape(code)}(?![A-Za-z])")
        else:
            pattern = re.compile(re.escape(code))
        if not pattern.search(out):
            continue
        target = CODES[code]
        repl = f"{code}（{target}）" if keep_original else target
        out = pattern.sub(repl, out)
    return out


def load(path: Path | None = None) -> dict:
    """加载外部词表（优先），与内置表合并。"""
    p = path or DEFAULT_PATH
    data = {"schools": list(SCHOOLS), "terms": list(TERMS),
            "codes": dict(CODES), "corrections": dict(CORRECTIONS)}
    if p.exists():
        try:
            ext = json.loads(p.read_text(encoding="utf-8"))
            data["schools"] = list(dict.fromkeys(data["schools"] + ext.get("schools", [])))
            data["terms"] = list(dict.fromkeys(data["terms"] + ext.get("terms", [])))
            data["codes"].update(ext.get("codes", {}))
            data["corrections"].update(ext.get("corrections", {}))
            print(f"[lexicon] 已加载外部词表 {p.name}（+{len(ext.get('schools', []))} 校名）")
        except Exception as e:  # noqa: BLE001
            print(f"[lexicon] 外部词表加载失败，用内置表: {e}")
    return data


def apply_loaded(data: dict) -> None:
    """把 load() 的结果写回模块级表，供全局使用。"""
    global SCHOOLS, TERMS, CODES, CORRECTIONS
    SCHOOLS = data["schools"]
    TERMS = data["terms"]
    CODES = data["codes"]
    CORRECTIONS = data["corrections"]
