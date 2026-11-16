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
