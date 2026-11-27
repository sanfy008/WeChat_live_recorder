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
    "省市用品": "省实永平",
    "盛世用平": "省实永平",
    "省实用品": "省实永平",
    "陈氏用品": "省实永平",
    "云城培营": "云城培英",
    "云山赔英": "云城培英",
    "云省培英": "云城培英",
    "云神培林": "云城培英",
    "金广复": "金广附",
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
    "华初中学": "黄埔华初",
    "东江外国语学校": "东江外国语",
}

AMBIGUOUS_CODES: dict[str, list[str]] = {}
ASR_PRIORITY_SCHOOLS: list[str] = []
ASR_PRIORITY_TERMS: list[str] = []
SCHOOL_ALIASES: dict[str, list[str]] = {}
_URL_SPAN_RE = re.compile(r"(https?://\S+)")

# 单独出现的"家"需谨慎替换（"大家""家长"都是正常词），
# 所以只在数字后紧跟"家"时替换
_RE_NUM_JIA = re.compile(r"(\d{3})\s*家")

# 繁→简 常用字兜底表（覆盖实测出现的高频字，opencc 缺失时启用）
#
# 为什么需要自己维护：本机没有 opencc，实际走的一直是这张表。
# 2026-11-26 复盘 7 份产物时统计出「终稿里仍有、且本表修不掉」的繁体字共
# 186 处 / 7 个字：級(71) 師(45) 階(27) 體(19) 聯(13) 繫(10) 韓(1)，
# 全部是升学直播高频词（年级/老师/进阶/体验/联系/寒假/韩）。
# 新增字务必只放「简体里几乎不会自然出现的繁体字形」，否则会误伤正常简体文本。
# 判据：`python scripts/lexicon_retro.py --all` 的第 1 项应为 0。
_TC2SC = str.maketrans(
    "個們這來說時會後裡麼為與對開關實現點線樣區問間還進過發現當從學經濟這樣沒電話"
    "讓給帶東車長門見覺聽講讀寫買賣價錢銀藝術動員員國圖書畫筆記億萬歲歷經歷"
    # 2026-11-26 复盘补：密集出现在升学直播口播里的繁体字
    "級師階體聯繫韓",
    "个们这来说时会后里么为与对开关实现点线样区问间还进过发现当从学经济这样没电话"
    "让给带东车长门见觉听讲读写买卖价钱银艺术动员员国图书画笔记亿万岁历经历"
    "级师阶体联系寒",
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
    优先使用外部词表指定的 asr_priority_schools / asr_priority_terms，避免 [:40] 切片截断外部校名。
    """
    school_pool = ASR_PRIORITY_SCHOOLS or SCHOOLS
    term_pool = ASR_PRIORITY_TERMS or TERMS
    schools = "、".join(school_pool[:50])
    terms = "、".join(term_pool[: max(0, max_terms - 50)])
    return (
        f"本音频为广州小升初(XSC)升学规划直播。"
        f"专有名词参考：{schools}。"
        f"术语参考：{terms}。"
        f"上述仅为识别参考，不要朗读或复述这些词。"
    )


def build_vocabulary(school_limit: int = 60, term_limit: int = 40) -> str:
    """生成供 LLM 纠错使用的领域词表串（优先取 asr_priority_* 防止外部 139+ 校名被切片截断）。"""
    school_pool = ASR_PRIORITY_SCHOOLS or SCHOOLS
    term_pool = ASR_PRIORITY_TERMS or TERMS
    picked = list(dict.fromkeys(school_pool[:school_limit] + term_pool[:term_limit]))
    return "、".join(picked)


def correct_text(text: str) -> str:
    """按实测错误表做纠正。长词优先，避免"省实"被"省"截断。

    数字保护与 URL 保护：
    - 中文数字/阿拉伯数字紧邻时不做替换（"一百六十" 不误改为 "一白六十"）
    - URL 片段原样跳过
    """
    if not text:
        return text
    sorted_rules = sorted(CORRECTIONS, key=len, reverse=True)
    parts = _URL_SPAN_RE.split(text)
    out_parts: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            out_parts.append(part)
            continue
        out = part
        for wrong in sorted_rules:
            right = CORRECTIONS[wrong]
            if wrong == right or wrong not in out:
                continue
            out = _replace_guarded(out, wrong, right)
        out_parts.append(_num_jia_fix(out))
    return "".join(out_parts)


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


def _find_protected_spans(text: str, protected_desc: list[str]) -> list[tuple[int, int]]:
    """查找文本中已出现的正式校名/完整目标名跨度（长词优先去重叠），防止短代码切碎正式名称。"""
    n = len(text)
    occupied = [False] * n
    spans: list[tuple[int, int]] = []
    for term in protected_desc:
        tlen = len(term)
        if tlen > n:
            continue
        is_ascii = term.isascii()
        start = 0
        while True:
            idx = text.find(term, start)
            if idx == -1:
                break
            end = idx + tlen
            if is_ascii:
                prev_ch = text[idx - 1] if idx > 0 else ""
                next_ch = text[end] if end < n else ""
                if (prev_ch.isascii() and (prev_ch.isalnum() or prev_ch == "_")) or (
                    next_ch.isascii() and (next_ch.isalnum() or next_ch == "_")
                ):
                    start = idx + 1
                    continue
            if not any(occupied[idx:end]):
                for k in range(idx, end):
                    occupied[k] = True
                spans.append((idx, end))
            start = end
    return spans


def expand_codes(text: str, keep_original: bool = True) -> str:
    """把暗语展开成正式校名，便于后续 LLM 理解和检索。

    keep_original=True 时输出 `暗语（正式名）`，保留原文可追溯。

    实现要点：
    1. 单次从左到右扫描 + 最左最长优先，不回头处理已展开内容
    2. 屏蔽 URL（https?://...），避免链接内部字母被误展开
    3. ASCII 代码按 `isalnum() or == '_'` 检验词边界，避免 `65TY`/`2022Z` 误展开
    4. 若 `matched == target`（如 `YMO` -> `YMO`），不加自括号 `YMO（YMO）`
    5. 正式校名与目标名保护：若命中跨度被更长的已知校名/别名/目标名覆盖（如 `华侨外国语` 内的 `侨外`、`滨海实验` 内的 `海实`、`黄埔华初` 内的 `华初`、`石楼中学` 内的 `石楼`），跳过短代码展开
    """
    if not text:
        return text

    codes = sorted((c for c in CODES if c not in _NO_EXPAND), key=len, reverse=True)
    protected_set: set[str] = (
        {s.strip() for s in SCHOOLS if isinstance(s, str) and s.strip()}
        | _NO_EXPAND
        | {v.strip() for v in CODES.values() if isinstance(v, str) and v.strip()}
    )
    for can, aliases in SCHOOL_ALIASES.items():
        if isinstance(can, str) and len(can.strip()) >= 4:
            protected_set.add(can.strip())
        for a in aliases:
            if isinstance(a, str) and len(a.strip()) >= 4 and not a.strip().isascii():
                protected_set.add(a.strip())
    protected_desc = sorted(protected_set, key=len, reverse=True)
    protected_spans = _find_protected_spans(text, protected_desc)

    url_spans = {m.start(): m.end() for m in _URL_SPAN_RE.finditer(text)}
    n = len(text)
    out: list[str] = []
    i = 0
    while i < n:
        if i in url_spans:
            end = url_spans[i]
            out.append(text[i:end])
            i = end
            continue

        matched: str | None = None
        for code in codes:
            if not text.startswith(code, i):
                continue
            clen = len(code)
            # 纯字母数字代码要求词边界，避免 "SS" / "65TY" / "2022Z" 误伤
            if code.isascii():
                prev_ch = text[i - 1] if i > 0 else ""
                next_ch = text[i + clen] if i + clen < n else ""
                if prev_ch.isascii() and (prev_ch.isalnum() or prev_ch == "_"):
                    continue
                if next_ch.isascii() and (next_ch.isalnum() or next_ch == "_"):
                    continue
            # 更长正式校名/目标名跨度保护：禁止内部/后缀短代码切碎正式名称
            cend = i + clen
            if any((not (cend <= ps or i >= pe)) and clen < (pe - ps) for ps, pe in protected_spans):
                continue
            # 后缀冲突保护：`金奥` 不能吃掉 `小金奥班` 里的 `金奥`。
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

        # 自映射（如 YMO -> YMO）直接原样保留，不加括号
        if target == matched:
            out.append(matched)
            i += len(matched)
            continue

        # 中文后缀保护：若原文此处已是完整目标名（如 "石楼中学"），原样跳过
        if target.startswith(matched) and text.startswith(target, i):
            out.append(target)
            i += len(target)
            continue

        # 幂等保护：源文本若已是 `code（正式名）` 形式，直接原样保留，不再追加一层。
        already = f"{matched}（{target}）"
        if text.startswith(already, i):
            out.append(already)
            i += len(already)
            continue

        out.append(f"{matched}（{target}）" if keep_original else target)
        i += len(matched)

    return "".join(out)


def _clean_map(d: dict) -> dict:
    """过滤掉分组标记、空值与 k == v 的无意义 NO-OP。"""
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


def _clean_ambiguous(d: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in (d or {}).items():
        if not isinstance(k, str) or not k.strip() or k.startswith("_"):
            continue
        if isinstance(v, list):
            cands = [str(x).strip() for x in v if str(x).strip()]
            if cands:
                out[k.strip()] = cands
    return out


def load(path: Path | None = None) -> dict:
    """加载外部词表（优先），与内置表合并。

    - 支持 asr_priority_schools / asr_priority_terms 并将高优校名/术语前置，防止切片截断
    - 支持 ambiguous_codes，并从合并后的 codes 中剔除外部已标明多义的代码，阻止内置硬编码穿透复活
    - 支持 corrections_common + corrections_asr_only 合并
    """
    explicit = path is not None
    p = path or DEFAULT_PATH
    data: dict = {
        "schools": list(SCHOOLS),
        "terms": list(TERMS),
        "codes": dict(CODES),
        "corrections": {k: v for k, v in CORRECTIONS.items() if k != v},
        "ambiguous_codes": dict(AMBIGUOUS_CODES),
        "asr_priority_schools": list(ASR_PRIORITY_SCHOOLS),
        "asr_priority_terms": list(ASR_PRIORITY_TERMS),
        "school_aliases": {},
    }
    if p.exists():
        try:
            ext = json.loads(p.read_text(encoding="utf-8"))
            prio_schools = [s for s in ext.get("asr_priority_schools", []) if isinstance(s, str) and s.strip()]
            prio_terms = [t for t in ext.get("asr_priority_terms", []) if isinstance(t, str) and t.strip()]
            ext_schools = [s for s in ext.get("schools", []) if isinstance(s, str) and s.strip()]
            ext_terms = [t for t in ext.get("terms", []) if isinstance(t, str) and t.strip()]

            data["asr_priority_schools"] = list(dict.fromkeys(prio_schools))
            data["asr_priority_terms"] = list(dict.fromkeys(prio_terms))
            data["schools"] = list(dict.fromkeys(prio_schools + data["schools"] + ext_schools))
            data["terms"] = list(dict.fromkeys(prio_terms + data["terms"] + ext_terms))
            raw_aliases = ext.get("school_aliases", {})
            if isinstance(raw_aliases, dict):
                data["school_aliases"] = {
                    k.strip(): [a.strip() for a in v if isinstance(a, str) and a.strip()]
                    for k, v in raw_aliases.items()
                    if isinstance(k, str) and k.strip() and not k.startswith("_") and isinstance(v, list)
                }

            ext_codes = _clean_map(ext.get("codes", {}))
            data["codes"].update(ext_codes)

            amb = _clean_ambiguous(ext.get("ambiguous_codes", {}))
            data["ambiguous_codes"] = amb
            amb_norm = {k.upper() if k.isascii() else k for k in amb}
            pending = {
                k for k in (ext.get("_待确认") or {})
                if isinstance(k, str) and not k.startswith("_")
            }
            for ck in list(data["codes"].keys()):
                ck_norm = ck.upper() if ck.isascii() else ck
                if ck_norm in amb_norm or ck in pending:
                    data["codes"].pop(ck, None)

            ext_corr: dict[str, str] = {}
            ext_corr.update(_clean_map(ext.get("corrections", {})))
            ext_corr.update(_clean_map(ext.get("corrections_common", {})))
            ext_corr.update(_clean_map(ext.get("corrections_asr_only", {})))
            for k, v in ext_corr.items():
                if k != v:
                    data["corrections"][k] = v

            print(f"[lexicon] 已加载外部词表 {p.name}（+{len(ext_schools)} 校名，{len(amb)} 多义隔离）")
        except Exception as e:  # noqa: BLE001
            print(f"[lexicon][warn] 外部词表加载失败，用内置表: {e}")
    elif explicit:
        print(f"[lexicon][warn] 指定的词表文件不存在，回退内置表: {p}")
    return data


def apply_loaded(data: dict) -> None:
    """把 load() 的结果写回模块级表，供全局使用。"""
    global SCHOOLS, TERMS, CODES, CORRECTIONS, AMBIGUOUS_CODES, ASR_PRIORITY_SCHOOLS, ASR_PRIORITY_TERMS, SCHOOL_ALIASES
    SCHOOLS = data["schools"]
    TERMS = data["terms"]
    CODES = data["codes"]
    CORRECTIONS = data["corrections"]
    AMBIGUOUS_CODES = data.get("ambiguous_codes", {})
    ASR_PRIORITY_SCHOOLS = data.get("asr_priority_schools", [])
    ASR_PRIORITY_TERMS = data.get("asr_priority_terms", [])
    SCHOOL_ALIASES = data.get("school_aliases", {})
