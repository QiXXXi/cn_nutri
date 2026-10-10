"""拼音规范化：边界用例。

这些是整套系统的地基——韵母/声母拆错，所有拼音类查询都会错。
"""

import pytest

from cn_nutri.pinyin import (
    ALL_FINALS,
    ALL_INITIALS,
    Syllable,
    canonical_final,
    canonical_initial,
    format_pinyin,
    parse_syllable,
)

CASES = {
    # 数字调
    "zhang1": ("zh", "ang", 1),
    "ai4": ("", "ai", 4),
    "dui4": ("d", "ui", 4),      # uei -> ui
    "liu4": ("l", "iu", 4),      # iou -> iu
    "zhun3": ("zh", "un", 3),    # uen -> un
    "lv4": ("l", "v", 4),        # ü 写作 v
    "nv3": ("n", "v", 3),
    "er2": ("", "er", 2),
    # 零声母：y/w 不是声母，只是拼写形式
    "yu2": ("", "v", 2),         # yu 的韵母是 ü，不是 u
    "wu2": ("", "u", 2),         # wu 的韵母是 u
    "wen2": ("", "un", 2),
    "yi1": ("", "i", 1),
    "yao2": ("", "iao", 2),
    "you3": ("", "iu", 3),
    "yang2": ("", "iang", 2),
    "yong3": ("", "iong", 3),
    "weng1": ("", "ueng", 1),
    "yuan2": ("", "van", 2),
    "yue4": ("", "ve", 4),
    "yun2": ("", "vn", 2),
    # j/q/x 后的 u 实际是 ü
    "juan4": ("j", "van", 4),
    "xue2": ("x", "ve", 2),
    "qun2": ("q", "vn", 2),
    "ju4": ("j", "v", 4),
    # 儿化后缀
    "r5": ("", "er", 5),
}

# 调号写法
TONE_MARK_CASES = {
    "zhāng": ("zh", "ang", 1),
    "yàng": ("", "iang", 4),
    "lǜ": ("l", "v", 4),
    "nǚ": ("n", "v", 3),
    "yú": ("", "v", 2),
    "wén": ("", "un", 2),
    "ér": ("", "er", 2),
    "yī": ("", "i", 1),
}


@pytest.mark.parametrize("raw,want", CASES.items())
def test_parse_numbered(raw, want):
    assert tuple(parse_syllable(raw)) == want


@pytest.mark.parametrize("raw,want", TONE_MARK_CASES.items())
def test_parse_tone_marks(raw, want):
    assert tuple(parse_syllable(raw)) == want


def test_numbered_and_marked_agree():
    assert parse_syllable("zhang1") == parse_syllable("zhāng")


def test_case_insensitive_and_trimmed():
    assert parse_syllable("  ZHANG1 ") == Syllable("zh", "ang", 1)


def test_uei_uen_iou_normalized_to_written_form():
    """内部一律存书写形，用户写全写形也应命中。"""
    for full, short in (("uei", "ui"), ("uen", "un"), ("iou", "iu")):
        assert canonical_final(full) == short == canonical_final(short)


def test_umlaut_spellings_all_normalize():
    for spelling in ("v", "ü", "u:"):
        assert canonical_final(spelling) == "v"
    assert tuple(parse_syllable("nü3")) == ("n", "v", 3)


def test_invalid_syllables_raise():
    for bad in ("", "xyz", "zzz9", "hello", "12"):
        with pytest.raises(ValueError):
            parse_syllable(bad)


def test_initial_and_final_tables_are_disjoint():
    """查询语法「不同类条件不歧义」的基础：两表交集必须为空。"""
    assert not (set(ALL_INITIALS) & ALL_FINALS)


def test_zero_initial_aliases():
    for spelling in ("xx", "", "∅", "0"):
        assert canonical_initial(spelling) == ""


def test_format_pinyin_zero_initial():
    """零声母要还原成 y/w 拼写，否则「样」会显示成 iàng。"""
    assert format_pinyin("", "iang", 4) == "yàng"
    assert format_pinyin("", "un", 2) == "wén"
    assert format_pinyin("", "v", 2) == "yú"
    assert format_pinyin("", "u", 3) == "wǔ"


def test_format_pinyin_jqx_drops_umlaut():
    """j/q/x 后面的 ü 不写两点。"""
    assert format_pinyin("j", "v", 4) == "jù"
    assert format_pinyin("q", "van", 1) == "quān"
    assert format_pinyin("x", "vn", 1) == "xūn"
    # 但 n/l 后面保留
    assert format_pinyin("n", "v", 3) == "nǚ"
    assert format_pinyin("l", "v", 4) == "lǜ"


def test_format_pinyin_tone_placement():
    assert format_pinyin("zh", "ang", 1) == "zhāng"    # a 优先
    assert format_pinyin("g", "ui", 4) == "guì"        # 无 a/o/e -> 末位元音
    assert format_pinyin("l", "iu", 4) == "liù"
    assert format_pinyin("h", "uo", 3) == "huǒ"        # o 优先于 u


def test_format_pinyin_neutral_tone_has_no_mark():
    assert format_pinyin("d", "e", 5) == "de"


@pytest.mark.parametrize("initial,final,tone",
                         [(i, f, t) for i in ALL_INITIALS for f in ("a", "ang", "v")
                          for t in (1, 2, 3, 4)])
def test_format_then_parse_roundtrip(initial, final, tone):
    """展示形必须能被解析回同一个音节。"""
    shown = format_pinyin(initial, final, tone)
    assert tuple(parse_syllable(shown)) == (initial, final, tone)
