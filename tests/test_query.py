"""查询语言解析：分类、全半角归一、无法识别项、字面集。"""

import pytest

from cn_nutri.query import (
    AnyChar,
    CharConstraint,
    Literal,
    Slot,
    Star,
    parse_pattern,
)


def one(src):
    """解析一个只含单个字位的表达式，返回它的约束。"""
    p = parse_pattern(src)
    slots = [e for e in p.elements if isinstance(e, Slot)]
    assert len(slots) == 1, p.elements
    return slots[0].constraint


# ---------------------------------------------------------------- 基本题意

def test_task_example():
    """题目：第1字韵母∈{ai,an,ang}，第2字三/四声，第3字≤7画，第4字独体或左右。"""
    p = parse_pattern("[a,an,ang][~3,~4][7-][独体,左右]")
    assert p.fixed_length() == 4
    assert not p.errors and not p.ignored

    a, b, c, d = (e.constraint for e in p.elements)
    assert a.finals == {"a", "an", "ang"}
    assert b.tones == {3, 4}
    assert c.strokes == (("le", 7),)
    assert d.structures == {"独体", "左右"}


@pytest.mark.parametrize("final", ["a", "an", "ang"])
def test_task_example_first_slot_matches(final):
    c = parse_pattern("[a,an,ang]").elements[0].constraint
    assert final in c.finals


# ------------------------------------------------------- 完整示例（§九.2）

FULL_EXAMPLE = "[氵，m ,a, e，~3,b,左右，8-,(沐露二勾恶感),10，i,  xx ， 上下，" \
               "xx, ~4，u，v,16+,o，这是啥, idontknow】"


def test_full_example_parses_to_seven_categories():
    p = parse_pattern(FULL_EXAMPLE)
    c = p.elements[0].constraint
    classes = [c.finals, c.initials, c.tones, c.strokes,
               c.structures, c.parts, c.literals]
    assert all(classes), "七类约束都应非空"


def test_full_example_exact_values():
    c = one(FULL_EXAMPLE)
    assert c.finals == {"a", "e", "i", "u", "v", "o"}
    assert c.tones == {3, 4}
    assert c.initials == {"", "m", "b"}          # "" = 零声母（xx）
    assert c.structures == {"上下", "左右"}
    assert c.parts == {"氵"}
    assert c.literals == {"沐", "露", "二", "勾", "恶", "感"}
    assert sorted((op, n) for op, n in c.strokes) == [("eq", 10), ("ge", 16), ("le", 8)]


def test_full_example_ignores_exactly_two_items():
    p = parse_pattern(FULL_EXAMPLE)
    assert [i.text for i in p.ignored] == ["这是啥", "idontknow"]
    assert not p.errors


def test_full_example_stroke_expansion():
    c = one(FULL_EXAMPLE)
    s = c.stroke_set()
    assert 0 in s and 8 in s and 10 in s and 16 in s and 64 in s
    assert 9 not in s and 11 not in s and 15 not in s


# ------------------------------------------------------------- 符号归一化

@pytest.mark.parametrize("full,half", [
    ("［a］", "[a]"),
    ("[～3]", "[~3]"),
    ("[7－]", "[7-]"),
    ("[16＋]", "[16+]"),
    ("（沐露）", "(沐露)"),
    ("[a，e]", "[a,e]"),
    ("[a、e]", "[a,e]"),
])
def test_fullwidth_symbols_are_equivalent(full, half):
    assert parse_pattern(full).to_dsl() == parse_pattern(half).to_dsl()


def test_spaces_are_ignored():
    assert parse_pattern("[ a , an ]").to_dsl() == parse_pattern("[a,an]").to_dsl()


def test_ideographic_space_ignored():
    assert parse_pattern("[a　e]").to_dsl() == "[a,e]"


# --------------------------------------------------------------- 分类细节

@pytest.mark.parametrize("token,field,value", [
    ("a", "finals", "a"),
    ("ang", "finals", "ang"),
    ("ü", "finals", "v"),
    ("v", "finals", "v"),
    ("ng", "finals", "ng"),
    ("b", "initials", "b"),
    ("zh", "initials", "zh"),
    ("xx", "initials", ""),
    ("独体", "structures", "独体"),
    ("半包围", "structures", "半包围"),
    ("形声", "liushu", "形声"),
    ("会意", "liushu", "会意"),
    ("氵", "parts", "氵"),
    ("日", "parts", "日"),
])
def test_token_classification(token, field, value):
    c = one(f"[{token}]")
    assert value in getattr(c, field)


@pytest.mark.parametrize("token,op,n", [
    ("8", "eq", 8), ("8-", "le", 8), ("16+", "ge", 16),
])
def test_stroke_token_classification(token, op, n):
    assert one(f"[{token}]").strokes == ((op, n),)


@pytest.mark.parametrize("token,tone", [
    ("~1", 1), ("~2", 2), ("~3", 3), ("~4", 4), ("~5", 5), ("~0", 5),
])
def test_tone_tokens(token, tone):
    assert one(f"[{token}]").tones == {tone}


def test_ids_string_is_single_constraint():
    c = one("[⿰弓长]")
    assert c.ids == {"⿰弓长"}
    assert not c.finals and not c.parts


def test_literal_set():
    assert one("[(沐露二勾恶感)]").literals == {"沐", "露", "二", "勾", "恶", "感"}


def test_same_class_union_cross_class_intersection():
    c = one("[a,e,~3,~4,7-]")
    assert c.finals == {"a", "e"}
    assert c.tones == {3, 4}
    assert c.strokes == (("le", 7),)


# --------------------------------------------------------------- 通配与字面

def test_wildcards():
    p = parse_pattern("*[左右][7-]*")
    assert isinstance(p.elements[0], Star)
    assert isinstance(p.elements[-1], Star)
    assert p.fixed_length() is None
    assert sum(isinstance(e, Slot) for e in p.elements) == 2


def test_question_mark():
    p = parse_pattern("?[a]")
    assert isinstance(p.elements[0], AnyChar)
    assert p.fixed_length() == 2


def test_bare_han_is_literal():
    p = parse_pattern("张[a,an]")
    assert isinstance(p.elements[0], Literal) and p.elements[0].ch == "张"
    assert p.elements[1].constraint.finals == {"a", "an"}


def test_escape():
    p = parse_pattern(r"\[")
    assert isinstance(p.elements[0], Literal) and p.elements[0].ch == "["


def test_consecutive_stars_collapse():
    p = parse_pattern("**")
    assert sum(isinstance(e, Star) for e in p.elements) == 1


# ------------------------------------------------------------------ 其它

def test_empty_brackets_mean_any_char():
    c = one("[]")
    assert isinstance(c, CharConstraint) and c.is_empty()


def test_all_unrecognized_bracket_is_any_char():
    c = one("[这是啥,idontknow]")
    assert c.is_empty()


def test_unclosed_bracket_is_error():
    p = parse_pattern("[a,an")
    assert p.errors and "未闭合" in p.errors[0].reason


def test_roundtrip_through_dsl():
    for src in ("[a,an][~3][7-][独体,左右]", "张[a]", "*[氵]*",
                "[⿰弓长][~1]", "[(沐露)]"):
        once = parse_pattern(src).to_dsl()
        twice = parse_pattern(once).to_dsl()
        assert once == twice, src


def test_structure_alias_dsl_is_short_form():
    """DSL 里结构用「独体」而不是汉典的「单一结构」。"""
    assert one("[独体]").to_dsl() == "[独体]"


# ------------------------------------------------------------- 相等/不等标记

def test_eq_neq_token_classification():
    c = one("[=a,≠B]")
    assert c.eq == {"A"} and c.neq == {"B"}
    assert not c.is_empty()


def test_labels_are_uppercased():
    assert one("[=a]").eq == one("[=A]").eq == {"A"}


def test_bang_eq_is_an_alias_for_neq():
    """`!=A` 是给键盘的退路：≠ 在多数输入法里不好打。"""
    p = parse_pattern("[!=A][=A]")
    assert p.errors == []
    assert p.elements[0].constraint.neq == {"A"}
    assert p.to_dsl() == "[≠A][=A]"


def test_fullwidth_marks_normalize():
    p = parse_pattern("[＝Ａ][！＝Ａ]")
    assert p.normalized == "[=A][!=A]"
    assert p.errors == []
    assert p.to_dsl() == "[=A][≠A]"


@pytest.mark.parametrize("src", ["[=AB]", "[=1]", "[=]", "[=汉]"])
def test_bad_label_is_unrecognized_not_an_error(src):
    """标签必须是单个 ASCII 字母；否则退回通用的「无法识别」路径。"""
    p = parse_pattern(src)
    assert p.ignored and p.ignored[0].reason == "无法识别"
    assert p.errors == []


def test_neq_before_eq_is_legal():
    """悬空义务：≠A 允许写在 =A 之前，由 matcher 兜住。"""
    p = parse_pattern("[≠A][=A]")
    assert p.errors == []


def test_neq_without_any_eq_is_an_error():
    p = parse_pattern("[≠A][~1]")
    assert len(p.errors) == 1
    assert p.errors[0].reason == "≠A 没有对应的 =A"
    assert p.errors[0].offset == 0


def test_eq_and_neq_on_the_same_slot_is_an_error():
    p = parse_pattern("[=A,≠A]")
    assert len(p.errors) == 1
    assert p.errors[0].reason == "=A 与 ≠A 不能写在同一字位"


def test_same_slot_eq_neq_is_allowed_when_other_slots_define_it():
    """`[=A,≠A]` 单看自相矛盾，但整个表达式里 A 由别处定义——仍然非法。

    非法性只看**同一个字位**，跟别处有没有定义无关：那个字位永远无法满足。
    """
    p = parse_pattern("[=A][=A,≠A]")
    assert len(p.errors) == 1
    assert p.errors[0].offset == 4


def test_marks_may_coexist_with_other_conditions():
    c = one("[=A,~3]")
    assert c.eq == {"A"} and c.tones == {3}


def test_mark_dsl_roundtrip():
    """往返稳定：正则在第一次序列化后收敛，之后再解析不再变。"""
    for src in ("[=A][=A]", "[=A][][=A][≠A]", "[≠A][=A]", "[=A,~3,≠B][=B]",
                "[=A][=B][=A][=B]"):
        once = parse_pattern(src).to_dsl()
        assert parse_pattern(once).to_dsl() == once, src
        assert parse_pattern(once).errors == []


def test_marks_sort_before_other_conditions_in_dsl():
    """to_dsl 的项序就是文本框的内容，标记必须固定排在最前，不然会自己跳。"""
    assert one("[~3,=B,≠A,a,7]").to_dsl() == "[=B,≠A,~3,7,a]"
