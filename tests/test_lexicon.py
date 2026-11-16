"""XSC 领域词表测试。

覆盖三类能力：
1. 错字纠正（来自真实直播的实测错误对）
2. 暗语展开（暗语 → 暗语（正式名））
3. 不误伤（正常词语不能被改坏）
"""

from __future__ import annotations

from wxlive import lexicon


def test_fix_xiaoshi_corrections():
    """省实相关的实测错字都要能修回来。"""
    cases = {
        "省市用品有付费MK吗": "省实有付费MK吗",
        "盛世用平有附中MK吗": "省实有附中MK吗",
        "陈氏用品有复分MK吗": "省实有复分MK吗",
    }
    for wrong, right in cases.items():
        assert lexicon.correct_text(wrong) == right, f"失败: {wrong}"


def test_fix_school_names():
    assert lexicon.correct_text("云城培营考初中内容") == "云城培英考初中内容"
    assert lexicon.correct_text("云山赔英考初中") == "云城培英考初中"
    assert lexicon.correct_text("金广复和六") == "金广附和六"


def test_fix_terms():
    assert lexicon.correct_text("指标的道校") == "指标到校"
    assert lexicon.correct_text("明半学校") == "民办学校"
    assert lexicon.correct_text("公半的话") == "公办的话"
    assert lexicon.correct_text("抗礼在学校") == "靠前在学校"


def test_fix_other_measured_errors():
    assert lexicon.correct_text("它行驾会有一个东丽银") == "他寒假会有一个冬令营"
    assert lexicon.correct_text("再去胖道好不好") == "再去判断好不好"
    assert lexicon.correct_text("MKY区的一二线") == "MK外区的一二线"


def test_number_plus_sign():
    """730家 → 730+，但不能把"大家""家长"改成"大+""长+"。"""
    assert lexicon.correct_text("一线是均分730家") == "一线是均分730+"
    assert lexicon.correct_text("二线的话只是670家") == "二线的话只是670+"
    # 正常词不能被改
    assert lexicon.correct_text("大家好") == "大家好"
    assert lexicon.correct_text("家长你这个") == "家长你这个"


def test_no_false_positive_on_normal_text():
    t = "各位朋友大家好，欢迎来到今天的直播间，今天我给大家介绍三款产品。"
    assert lexicon.correct_text(t) == t


def test_expand_chinese_codes():
    out = lexicon.expand_codes("景观房怎么样")
    assert "金广附" in out, f"暗语未展开: {out}"
    assert "景观房" in out, "应保留原暗语以便追溯"

    out2 = lexicon.expand_codes("鸡腿和芝麻鸡哪个好")
    assert "铁一" in out2 and "六中" in out2


def test_expand_ascii_codes_require_word_boundary():
    """纯字母代码要按单词边界匹配，不能误伤长单词。"""
    out = lexicon.expand_codes("SS 和 HF 都不错")
    assert "省实" in out and "华附" in out
    # 长单词里包含 SS 不应被替换
    assert lexicon.expand_codes("CLASS") == "CLASS"


def test_expand_without_keeping_original():
    out = lexicon.expand_codes("景观房", keep_original=False)
    assert out == "金广附"


def test_empty_input_safe():
    assert lexicon.correct_text("") == ""
    assert lexicon.expand_codes("") == ""


def test_prompt_hint_contains_key_entities():
    hint = lexicon.build_prompt_hint()
    for word in ["省实", "金广附", "指标到校", "XSC"]:
        assert word in hint, f"prompt 提示缺少 {word}"
    # 必须提醒模型不要复读，否则会造成幻觉
    assert "不要朗读" in hint or "不要复述" in hint


def test_prompt_hint_length_reasonable():
    """prompt 太长会稀释模型注意力，控制在合理范围。"""
    hint = lexicon.build_prompt_hint()
    assert 200 < len(hint) < 2000, f"prompt 长度 {len(hint)} 不合适"


def test_load_merges_external_dict(tmp_path):
    import json

    p = tmp_path / "ext.json"
    p.write_text(
        json.dumps(
            {"schools": ["测试中学"], "codes": {"测试暗语": "测试中学"},
             "corrections": {"测试错字": "测试正字"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    data = lexicon.load(p)
    assert "测试中学" in data["schools"]
    assert data["codes"]["测试暗语"] == "测试中学"
    assert data["corrections"]["测试错字"] == "测试正字"
    # 内置内容不能被覆盖掉
    assert "省实" in data["schools"]


# ================= 数字保护（2026-11-16 实测新增） =================

def test_numeral_protection_keeps_chinese_numbers():
    """"百六→白六" 不能把"一百六十"改成"一白六十"。

    实测坑：讲人数、天数、分数时这类表达很常见。
    """
    from wxlive.lexicon import correct_text
    assert correct_text("一百六十个人") == "一百六十个人"
    assert correct_text("三百六十五天") == "三百六十五天"
    assert correct_text("一千六百") == "一千六百"
    assert correct_text("两万三千六") == "两万三千六"


def test_numeral_protection_does_not_block_real_code():
    """保护不能过度：句首/句尾的"百六"仍要替换成"白六"。"""
    from wxlive.lexicon import correct_text
    assert correct_text("百六的课改班") == "白六的课改班"
    assert correct_text("百六中怎么样") == "白六中怎么样"


def test_empty_string_is_not_a_digit():
    """回归："" in "0123…" 恒为 True，曾导致句首词永不替换。"""
    from wxlive import lexicon
    assert ("" in lexicon._DIGIT_CHARS) is True   # 这是 Python 的行为
    # 但替换逻辑必须显式判空，不能让句首词被跳过
    assert lexicon._replace_guarded("百六", "百六", "白六") == "白六"


def test_xsc_json_loads_and_merges():
    """外部词表必须能加载并与内置表合并。"""
    from wxlive import lexicon
    before_schools = len(lexicon.SCHOOLS)
    data = lexicon.load()
    assert isinstance(data, dict), "词表应解析为 dict"
    lexicon.apply_loaded(data)
    assert len(lexicon.SCHOOLS) >= before_schools


def test_xsc_json_has_no_placeholder_leak():
    """词表里的推测项不能混进可用于替换的字段。"""
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "lexicon" / "xsc.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    for key in ("codes", "corrections"):
        for k, v in d.get(key, {}).items():
            assert k and v, f"{key} 里有空键值: {k!r}->{v!r}"
            assert "存疑" not in v, f"corrections 不应含存疑标注: {k}->{v}"


# ================= 暗语展开（2026-11-16 实测重写） =================

def test_expand_no_nested_parentheses():
    """正式名里含 code 自身时不能嵌套括号。

    实测坑：清实→"清远实验（清实）"曾展开成 "清实（清远实验（清实））"。
    """
    from wxlive.lexicon import expand_codes
    out = expand_codes("清实跟应元怎么选")
    assert out.count("（") == out.count("）"), f"括号不配平: {out}"
    assert "（清远实验（清实））" not in out, f"出现嵌套: {out}"


def test_expand_longest_match_wins():
    """"小金龙"不能被"小金"先吃掉。

    实测坑：曾输出 "小金（金广附）龙（金广附）"。
    """
    from wxlive.lexicon import expand_codes
    out = expand_codes("小金龙零班")
    assert out == "小金龙（金广附）零班", out
    # 关键：不能出现 "小金（" —— 说明被短词先吃掉了
    assert "小金（" not in out, f"短词抢先匹配: {out}"


def test_expand_does_not_rescan_expanded_text():
    """展开出来的正式名不能被再次匹配（防重复展开）。"""
    from wxlive.lexicon import expand_codes
    out = expand_codes("小金泽高")
    # 小金→金广附，泽高→金广附泽高班；不能出现三层
    assert out.count("金广附") <= 3, out
    assert "（金广附（" not in out, f"重复展开: {out}"


def test_expand_keeps_original_for_traceability():
    """keep_original=True 时原文必须保留在内。"""
    from wxlive.lexicon import expand_codes
    assert expand_codes("烧鸡") == "烧鸡（白云华赋）"
    assert expand_codes("烧鸡", keep_original=False) == "白云华赋"


def test_expand_is_idempotent():
    """反复展开不能越跑越脏。

    实测坑：源文本已含 `小金龙（金广附）` 时，再展开会变成
    `小金龙（金广附）（金广附）`。
    """
    from wxlive.lexicon import expand_codes
    once = expand_codes("小金龙零班")
    twice = expand_codes(once)
    thrice = expand_codes(twice)
    assert once == twice == thrice, f"不幂等: {once!r} / {twice!r} / {thrice!r}"


def test_expand_idempotent_on_multi_code_sentence():
    from wxlive.lexicon import expand_codes
    a = expand_codes("小金泽高怎么选")
    assert expand_codes(a) == a, f"多暗语句不幂等: {a!r}"


def test_full_real_transcript_no_artifacts():
    """用真实 1561 句转录跑完整流程，不能出现结构性异常。"""
    import json
    from pathlib import Path
    from wxlive import lexicon

    p = (Path(__file__).resolve().parent.parent / "output" / "transcripts")
    files = sorted(p.glob("*回放*.json"))
    if not files:
        import pytest
        pytest.skip("无真实转录样本")

    lexicon.apply_loaded(lexicon.load())
    d = json.loads(files[0].read_text(encoding="utf-8"))
    bad = []
    for s in d["segments"]:
        e = lexicon.expand_codes(lexicon.correct_text(s["text"]))
        if "（（" in e or "（）" in e:
            bad.append(e[:60])
    assert not bad, f"出现嵌套/空括号 {len(bad)} 处: {bad[:3]}"
