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


# --------------------------------------------------------- 相等/不等标记
#
# 词是给定的，所以标记比的全是**已经确定的字**，退化成已知字之间的比较，
# 不需要搜索。唯一要小心的是 ≠A 可能写在 =A 之前（悬空义务）。

def hit(m, text, syllables, query):
    return m.match(parse_pattern(query), word(text, syllables)) is not None


def s(*tones):
    """只关心声调时的偷懒读音：声母韵母随便填，反正标记不看它们。"""
    return [("", "a", t) for t in tones]


def test_eq_marks_force_the_same_character():
    """[=A][=A]：两个字必须是同一个字。"""
    m = matcher()
    assert hit(m, "沐沐", s(1, 1), "[=A][=A]")
    assert not hit(m, "沐江", s(1, 1), "[=A][=A]")


def test_mark_slot_still_honours_its_own_constraints():
    """[=A][=A,~3]：既要求同字，第 2 字还得是第三声。"""
    m = matcher()
    assert hit(m, "沐沐", s(1, 3), "[=A][=A,~3]")
    assert not hit(m, "沐沐", s(1, 1), "[=A][=A,~3]")


def test_unequal_marks_the_user_example():
    """[=A][][=A][≠A]：第 1、3 字相同，第 4 字与它们不同。"""
    m = matcher()
    assert hit(m, "沐江沐白", s(1, 1, 1, 1), "[=A][][=A][≠A]")
    assert not hit(m, "沐江沐沐", s(1, 1, 1, 1), "[=A][][=A][≠A]")
    assert not hit(m, "沐江白沐", s(1, 1, 1, 1), "[=A][][=A][≠A]")


def test_two_independent_label_groups():
    """[=A][=B][=A][=B]：1=3、2=4；至于 1 与 2 相等与否不限。"""
    m = matcher()
    assert hit(m, "沐江沐江", s(1, 1, 1, 1), "[=A][=B][=A][=B]")
    assert hit(m, "沐沐沐沐", s(1, 1, 1, 1), "[=A][=B][=A][=B]")
    assert not hit(m, "沐江沐白", s(1, 1, 1, 1), "[=A][=B][=A][=B]")


def test_lone_eq_label_constrains_nothing():
    """只出现一次、又没有配对的 ≠A 时，=A 退化为无条件。"""
    m = matcher()
    assert hit(m, "张白", s(1, 1), "[~1][=A]")
    assert hit(m, "沐江", s(1, 1), "[~1][=A]")


def test_neq_before_eq_is_settled_when_the_label_binds():
    """[≠A][=A] 合法：不等义务挂在前面，等 A 在后面绑上时回头兑现。"""
    m = matcher()
    assert hit(m, "张白", s(1, 1), "[≠A][=A]")
    assert not hit(m, "明明", s(1, 1), "[≠A][=A]")


def test_pending_obligation_with_an_intervening_slot():
    """[≠A][~2][=A]：义务跨越中间那个字位才兑现。"""
    m = matcher()
    assert hit(m, "张白明", s(1, 2, 1), "[≠A][~2][=A]")     # 张 ≠ 明
    assert not hit(m, "明明明", s(1, 2, 1), "[≠A][~2][=A]")  # 明 == 明


def test_one_slot_may_carry_several_marks():
    """[=A][≠B,≠C][=B][=C]：第 2 字既不等于 B 也不等于 C。"""
    m = matcher()
    assert hit(m, "张江白明", s(1, 1, 1, 1), "[=A][≠B,≠C][=B][=C]")
    assert not hit(m, "张白白明", s(1, 1, 1, 1), "[=A][≠B,≠C][=B][=C]")


def test_marks_compose_with_star():
    """含 * 时游标态进 memo 键；这里钉住语义正确。"""
    m = matcher()
    assert hit(m, "张明明白", s(1, 1, 1, 1), "*[=A][=A]*")   # 含叠字 明明
    assert not hit(m, "张白江沐", s(1, 1, 1, 1), "*[=A][=A]*")


def test_pending_obligations_are_part_of_the_memo_key():
    """★ 这条是 * 与标记同用时的关键：同一个 (ei, wi) 可能带着**不同的**
    未兑现义务被走到（义务记的是字位，不是标签）。键里漏掉义务，两条路就会
    互相借答案。
    """
    m = matcher()
    # 首字与末字不同即命中；全同时不命中。
    assert hit(m, "张明明", s(1, 1, 1), "*[≠A]*[=A]")
    assert hit(m, "明明张", s(1, 1, 1), "*[≠A]*[=A]")
    assert not hit(m, "明明明", s(1, 1, 1), "*[≠A]*[=A]")


def test_mark_conflict_is_a_plain_miss_not_missing_data():
    """标记冲突是**确定**的不匹配，不该被记进 excluded_missing（数据缺失）。"""
    m = matcher()
    w = word("沐江", s(1, 1))
    assert m.match(parse_pattern("[=A][=A]"), w) is None
    assert m.stats.excluded_missing == 0
    assert m.stats.missing_chars == set()

# ------------------------------------------------------------------- 子集
#
# 子集是第二条跨字位条件，也是**唯一成员可以有两个字**的一类：判定要看后一个字，
# 所以它跟标记一样走 _scan/_glob，不走 match_prepared（那个只看得到一个字）。
# 这里用自造的小词表测语义，不碰内置的 810 个姓氏——那条在 test_query.py 里守数据。
# 用的字都取自本文件顶上的 CHARS，否则会撞上「字符数据缺失」。

def submatcher(pairs):
    """pairs 形如 {"姓氏": ("沐", "张白")}。"""
    return Matcher(CHARS, pairs)


def test_two_char_member_needs_the_next_char():
    """★ 用户点名的语义：`[$姓氏][][][]` 靠「张白」这个两字成员命中，
    而元素位仍然只占**一个字位**（四个元素配四个字）。"""
    m = submatcher({"姓氏": ("沐", "张白")})
    assert hit(m, "张白沐江", s(1, 1, 1, 1), "[$姓氏][][][]")
    assert hit(m, "沐白张江", s(1, 1, 1, 1), "[$姓氏][][][]")     # 靠单字成员
    assert not hit(m, "白张沐江", s(1, 1, 1, 1), "[$姓氏][][][]")  # 「白张」不是成员


def test_member_only_matches_where_the_element_lands():
    """子集是**位置**判定：`[][$姓氏][]` 里「张白」得从第 2 个字位起。"""
    m = submatcher({"姓氏": ("张白",)})
    assert hit(m, "沐张白", s(1, 1, 1), "[][$姓氏][]")
    assert not hit(m, "张白沐", s(1, 1, 1), "[][$姓氏][]")


def test_a_two_char_member_does_not_swallow_the_next_slot():
    """两字成员只让**本字位**成立，不占掉下一个元素的位置。

    词是「张白日」，`[$姓氏][5][4]` 里第 2 位必须是「白」（5 画）——若子集把
    「白」吃掉了，第 2 位就会落到「日」上，4 画那条对不上。
    """
    m = submatcher({"姓氏": ("张白",)})
    assert hit(m, "张白日", s(1, 1, 1), "[$姓氏][5][4]")
    assert not hit(m, "张白沐", s(1, 1, 1), "[$姓氏][5][4]")   # 沐 7 画


def test_two_char_member_cannot_reach_past_the_end():
    """词尾那个字位只有单字成员能救——单独一个「白」不是成员。"""
    m = submatcher({"姓氏": ("张白",)})
    assert not hit(m, "张白", s(1, 1), "[][$姓氏]")
    m2 = submatcher({"姓氏": ("张白", "白")})
    assert hit(m2, "张白", s(1, 1), "[][$姓氏]")


def test_several_subsets_on_one_slot_are_a_union():
    """组内同类取并集：[$姓氏,$朝代] = 姓氏或朝代。"""
    m = submatcher({"姓氏": ("张",), "朝代": ("明",)})
    assert hit(m, "张", s(1), "[$姓氏,$朝代]")
    assert hit(m, "明", s(1), "[$姓氏,$朝代]")
    assert not hit(m, "沐", s(1), "[$姓氏,$朝代]")


def test_subset_composes_with_other_conditions():
    """异类取交集：既要属于子集，又得满足别的条件。"""
    m = submatcher({"姓氏": ("张", "白")})
    assert hit(m, "张白", s(1, 1), "[$姓氏][5]")
    assert not hit(m, "张日", s(1, 1), "[$姓氏][5]")   # 日 4 画
    assert not hit(m, "沐白", s(1, 1), "[$姓氏][5]")   # 沐 不在子集里


def test_subset_miss_is_a_plain_miss_not_missing_data():
    """子集不命中是**确定**的不匹配，不该被记进 excluded_missing。"""
    m = submatcher({"姓氏": ("张",)})
    assert m.match(parse_pattern("[$姓氏]"), word("江", s(1))) is None
    assert m.stats.excluded_missing == 0
    assert m.stats.missing_chars == set()


def test_subset_works_through_the_glob_path():
    """含 * 时走 _glob。子集不改变游标状态，所以不进 memo 键——但语义要一致。"""
    m = submatcher({"姓氏": ("张白",)})
    assert hit(m, "明张白日", s(1, 1, 1, 1), "*[$姓氏]*")
    assert not hit(m, "明白江日", s(1, 1, 1, 1), "*[$姓氏]*")


def test_match_char_refuses_a_subset_constraint():
    """★ match_char 只有一个字，判不了带子集的约束，必须返回 None。

    返回 True 才是灾难：match_prepared 会把子集当成「无条件」，于是
    `[$姓氏]` 匹配**所有**字——正是「把不知道当成满足」。"""
    c = parse_pattern("[$姓氏]").elements[0].constraint
    assert match_char(c, "江", None, info("江")) is None
    # 同一个约束不碰子集那一类时照常判得了
    plain = parse_pattern("[左右]").elements[0].constraint
    assert match_char(plain, "江", None, info("江", structure="左右")) is True


def test_matcher_without_the_named_subset_is_a_miss_not_a_crash():
    """名字由解析器过词表，正常到不了这里。真到不了时按「不命中」处理，
    不要抛 KeyError 炸掉整条查询——**而且要和 JS 侧一致**：engine.js 找不到
    名字时就是把这个字位判死。
    """
    m = Matcher(CHARS, {})
    assert m.match(parse_pattern("[$姓氏]"), word("张", s(1))) is None
    assert m.stats.excluded_missing == 0
