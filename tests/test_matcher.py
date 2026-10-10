"""匹配器：通配回溯、多音字、数据缺失的显式语义、部件并集。"""

import pytest

from cn_nutri.matcher import CharInfo, Matcher, Word, match_char
from cn_nutri.pinyin import Syllable
from cn_nutri.query import CharConstraint, parse_pattern


def info(ch, strokes=5, radical=None, structure=None, liushu=None,
         ids=None, comps=()):
    return CharInfo(ch, strokes, radical, structure, liushu, ids,
                    frozenset(comps), ())


CHARS = {
    "张": info("张", 7, "弓", "左右", "形声", "⿰弓长", ["弓", "长"]),
    "白": info("白", 5, "白", "独体", "象形", "⿻？日", ["日"]),
    "日": info("日", 4, "日", "独体", "象形", "⿴口一", ["日"]),
    "明": info("明", 8, "日", "左右", "会意", "⿰日月", ["日", "月"]),
    "沐": info("沐", 7, "氵", "左右", "形声", "⿰氵木", ["氵", "木"]),
    "江": info("江", 6, "氵", "左右", "形声", "⿰氵工", ["氵", "工"]),
    "中": info("中", 4, "丨"),          # 结构数据缺失
}


def word(text, syllables):
    """``syllables`` 是**这一套读音**的逐音节列表：[("zh","ang",1), ...]。

    多音词请用 ``polyword``——把「一个词的多个音节」和「一个词的多套读音」
    搞混，是这套数据结构最容易踩的坑。
    """
    one = tuple(Syllable(*s) for s in syllables)
    assert len(one) == len(text), "读音音节数必须等于词长"
    return Word(text, (one,), ("cedict",), 100, 0, 0.0, (), False, "")


def polyword(text, readings):
    """``readings`` 是**多套**读音，每套逐音节。"""
    reads = tuple(tuple(Syllable(*s) for s in r) for r in readings)
    for r in reads:
        assert len(r) == len(text), "每套读音的音节数都必须等于词长"
    return Word(text, reads, tuple("cedict" for _ in reads),
                100, 0, 0.0, (), False, "")


def matcher():
    return Matcher(CHARS)


# ------------------------------------------------------------------ 基础

def test_empty_constraint_matches_anything():
    assert match_char(CharConstraint(), "张", None, CHARS["张"]) is True


def test_literal_constraint():
    c = CharConstraint(literals=frozenset("张"))
    assert match_char(c, "张", None, CHARS["张"]) is True
    assert match_char(c, "白", None, CHARS["白"]) is False


def test_stroke_constraint():
    assert match_char(CharConstraint(strokes=(("le", 7),)), "张", None,
                      CHARS["张"]) is True
    assert match_char(CharConstraint(strokes=(("le", 6),)), "张", None,
                      CHARS["张"]) is False


def test_structure_constraint():
    c = CharConstraint(structures=frozenset({"左右"}))
    assert match_char(c, "张", None, CHARS["张"]) is True
    assert match_char(c, "白", None, CHARS["白"]) is False


def test_parts_is_union_of_radical_and_decomposition():
    """[日] 既要命中 明（部首是日）也要命中 白（拆作 ⿻？日）。"""
    c = CharConstraint(parts=frozenset({"日"}))
    assert match_char(c, "明", None, CHARS["明"]) is True   # 部首
    assert match_char(c, "白", None, CHARS["白"]) is True   # 部件
    assert match_char(c, "沐", None, CHARS["沐"]) is False


def test_radical_variant_forms_both_match():
    c = CharConstraint(parts=frozenset({"氵"}))
    assert match_char(c, "沐", None, CHARS["沐"]) is True
    assert match_char(c, "江", None, CHARS["江"]) is True


def test_ids_exact_match():
    c = CharConstraint(ids=frozenset({"⿰弓长"}))
    assert match_char(c, "张", None, CHARS["张"]) is True
    assert match_char(c, "明", None, CHARS["明"]) is False


# ------------------------------------------------------- 数据缺失是显式的

def test_missing_structure_is_unknown_not_false():
    """结构为 NULL 的字既不命中也不报错，交由调用方统计提示。"""
    c = CharConstraint(structures=frozenset({"独体"}))
    assert match_char(c, "中", None, CHARS["中"]) is None


def test_missing_data_does_not_block_non_structural_constraints():
    c = CharConstraint(structures=frozenset({"独体"}), literals=frozenset("中"))
    assert match_char(c, "中", None, CHARS["中"]) is None
    c2 = CharConstraint(literals=frozenset("中"))
    assert match_char(c2, "中", None, CHARS["中"]) is True


def test_missing_char_entry_is_unknown():
    c = CharConstraint(parts=frozenset({"日"}))
    assert match_char(c, "?", None, None) is None


def test_missing_structure_does_not_affect_non_structural_query():
    """「中」的笔画有、结构没有——不涉及结构的查询照样命中。"""
    m = matcher()
    p = parse_pattern("[~1][~1]")
    w = word("日中", [("r", "i", 1), ("zh", "ong", 1)])
    assert m.match(p, w) is not None
    assert m.stats.excluded_missing == 0


def test_unknown_structure_excludes_the_word():
    """结构未知时不能放行——放行等于把「不知道」当成「满足」，会静默答错。"""
    m = matcher()
    p = parse_pattern("[独体][~1]")
    w = word("中日", [("zh", "ong", 1), ("r", "i", 1)])
    assert m.match(p, w) is None
    assert m.stats.excluded_missing == 1
    assert m.stats.missing_chars == {"中"}


def test_missing_data_is_counted_per_word_not_per_char():
    m = matcher()
    p = parse_pattern("[独体][~1]")
    for _ in range(3):
        m.match(p, word("中日", [("zh", "ong", 1), ("r", "i", 1)]))
    assert m.stats.excluded_missing == 3
    assert m.stats.missing_chars == {"中"}      # 字去重，词计数


# ------------------------------------------------------------------ 通配

def test_glob_star_matches_any_length():
    m = matcher()
    p = parse_pattern("*[氵]*")
    w = word("沐江沐", [("m", "u", 4)] * 3)
    res = m.match(p, w)
    assert res is not None
    assert len(res.spans) == 1


def test_glob_anchored_at_both_ends():
    m = matcher()
    w = word("张白", [("zh", "ang", 1), ("b", "ai", 2)])
    assert m.match(parse_pattern("[~1][~2]"), w) is not None
    assert m.match(parse_pattern("[~2][~1]"), w) is None


def test_glob_question_mark_is_exactly_one():
    m = matcher()
    w = word("明张", [("m", "ing", 2), ("zh", "ang", 1)])
    assert m.match(parse_pattern("?[~1]"), w) is not None
    assert m.match(parse_pattern("??[~1]"), w) is None


def test_glob_literal():
    m = matcher()
    w = word("张白", [("zh", "ang", 1), ("b", "ai", 2)])
    assert m.match(parse_pattern("张[b]"), w) is not None
    assert m.match(parse_pattern("明[b]"), w) is None


# --------------------------------------------------------------- 多音字

def test_polyphone_resolved_by_word_reading():
    """银行 yínháng 与 行走 xíngzǒu——同一个「行」在两个词里读音不同。"""
    m = matcher()
    bank = word("银行", [("", "in", 2), ("h", "ang", 2)])
    walk = word("行走", [("x", "ing", 2), ("z", "ou", 3)])

    assert m.match(parse_pattern("银[~2]"), bank) is not None
    assert m.match(parse_pattern("银[iang,ang]"), bank) is not None
    # 同一个「行」，在「银行」里是 háng、在「行走」里是 xíng——韵母据此不同
    assert m.match(parse_pattern("银[ang]"), bank) is not None
    assert m.match(parse_pattern("[ing][~3]"), walk) is not None
    assert m.match(parse_pattern("[ing][~3]"), bank) is None
    assert m.match(parse_pattern("[~2][~2]"), walk) is None   # 走是第三声


def test_multiple_readings_any_satisfies():
    m = matcher()
    w = polyword("结果", [[("j", "ie", 2), ("g", "uo", 3)]])
    w2 = polyword("结果", [[("j", "ie", 1), ("g", "uo", 3)],
                           [("j", "ie", 2), ("g", "uo", 3)]])
    p = parse_pattern("[~1][~3]")
    assert m.match(p, w) is None
    res = m.match(p, w2)
    assert res is not None and res.read_index == 0


def test_read_index_reported_for_highlighting():
    m = matcher()
    w = polyword("大夫", [[("d", "a", 4), ("f", "u", 1)],
                          [("d", "ai", 4), ("f", "u", 5)]])
    res = m.match(parse_pattern("[ai][~5]"), w)
    assert res is not None and res.read_index == 1


def test_word_without_readings_still_matches_literals():
    m = matcher()
    w = Word("日中", (), (), 0, 1, 0.0, (), False, "")
    assert m.match(parse_pattern("日中"), w) is not None


def test_match_returns_none_when_too_short():
    m = matcher()
    w = word("张", [("zh", "ang", 1)])
    assert m.match(parse_pattern("[~1][~1]"), w) is None
