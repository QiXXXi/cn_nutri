"""查询语言：DSL 文本 <-> 约束模型。

设计要点
--------
一个字位写成 ``[...]``，组内**同类条件取并集，不同类条件取交集**。
不需要用不同括号区分类别，因为各类的书写形态天然不重叠：

    韵母  a an ang v …        声母  b m zh … （xx = 零声母）
    声调  ~1 ~2 ~3 ~4         笔画  8 = 恰八画 / 8- = ≤8 / 16+ = ≥16
    结构  独体 左右 上下 …     六书  象形 指事 会意 形声
    部件  裸写单字（氵 日 …）   拆字  ⿰弓长（以 IDS 算子开头）
    字面  (沐露二勾恶感)
    标记  =A 同字 / ≠A 不同字

``声母表 ∩ 韵母表 = ∅`` 是这个设计成立的前提，tests 里有断言守着。

标记是唯一**跨字位**的一类：``[=A][][=A][≠A]`` 表示第 1、3 字相同、第 4 字与它们
不同。``≠A`` 允许写在 ``=A`` 之前，非法只有一种——全式压根没有 ``=A``。

DSL 只是**序列化格式**，不是真相来源：网页表单直接构造同样的约束模型，
两条路径共用 matcher/rank，行为不会漂移。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Iterator

from .pinyin import (
    ALL_FINALS,
    ALL_INITIALS,
    ZERO_INITIAL,
    canonical_final,
    canonical_initial,
    display_final,
)

__all__ = [
    "CharConstraint", "Literal", "Slot", "AnyChar", "Star", "Pattern",
    "parse_pattern", "parse_constraint_group", "STRUCTURES", "LIUSHU_NAMES",
    "IDS_OPERATORS", "MAX_STROKES",
]

MAX_STROKES = 64

#: 结构标签。汉典写「单一结构」，这里用大家更熟的「独体」，解析时双向归一。
STRUCTURES = (
    "独体", "左右", "上下", "左中右", "上中下", "半包围", "全包围", "镶嵌", "品字",
)

LIUSHU_NAMES = ("象形", "指事", "会意", "形声", "转注", "假借")

#: IDS（表意文字描述序列）算子
IDS_OPERATORS = frozenset("⿰⿱⿲⿳⿴⿵⿶⿷⿸⿹⿺⿻⿼⿽⿾⿿")

# 全半角归一：全角 ASCII 段整体平移，另加几个中文标点。
# 注意「，～－＋」都在全角 ASCII 段内，已自动覆盖。
_PUNCT_MAP = {
    "【": "[", "】": "]", "〔": "[", "〕": "]",
    "、": ",", "；": ",", "·": ",",
    "　": " ", " ": " ",
}

_TONE_TOKENS = {"0": 5, "5": 5, "1": 1, "2": 2, "3": 3, "4": 4}


def _normalize_symbols(text: str) -> str:
    """全角转半角、中文标点归一到 DSL 符号。汉字与字母原样保留。"""
    out = []
    for ch in text:
        if ch in _PUNCT_MAP:
            out.append(_PUNCT_MAP[ch])
            continue
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:          # 全角 ASCII
            out.append(chr(cp - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


#: 汉字码位区间（基本区 + 扩展 A–I + 兼容区）。写成数据而不是一串 ``or``，
#: 是因为客户端引擎（web/engine.js）也要按同一张表判字，``CLIENT_TABLES`` 会把它
#: 原样导出，两边不会漂移。
HAN_RANGES = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF), (0x2A700, 0x2EBEF), (0x2F800, 0x2FA1F),
    (0x30000, 0x323AF),
)


def _is_han(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in HAN_RANGES)


#: 客户端引擎解析 DSL 需要的全部字表。``tools/build_client_data.py`` 把它连同
#: ``pinyin.CLIENT_TABLES`` 一起导成 ``web/data/tables.json``——JS 侧只移植**逻辑**，
#: 词汇表一律来自这里，将来加一个结构名或改一条全半角映射不必再改两处。
CLIENT_TABLES = {
    "structures": list(STRUCTURES),
    "liushu": list(LIUSHU_NAMES),
    "ids_operators": sorted(IDS_OPERATORS),
    "han_ranges": [list(r) for r in HAN_RANGES],
    "punct_map": dict(_PUNCT_MAP),
    "tone_tokens": dict(_TONE_TOKENS),
    "max_strokes": MAX_STROKES,
}


# --------------------------------------------------------------------------
# 约束模型
# --------------------------------------------------------------------------

#: 笔画比较： ("eq", 8) = 恰 8 画， ("le", 8) = ≤8， ("ge", 16) = ≥16
StrokeCmp = tuple[str, int]


@dataclass(frozen=True)
class CharConstraint:
    """一个字的全部约束。空集合表示该类别无约束（不是「空集」）。"""

    finals: frozenset[str] = frozenset()
    initials: frozenset[str] = frozenset()   # "" 成员表示零声母
    tones: frozenset[int] = frozenset()
    strokes: tuple[StrokeCmp, ...] = ()
    structures: frozenset[str] = frozenset()
    liushu: frozenset[str] = frozenset()
    parts: frozenset[str] = frozenset()      # 部首或部件，见 matcher 的并集语义
    ids: frozenset[str] = frozenset()        # 精确 IDS 串
    literals: frozenset[str] = frozenset()
    #: 相等/不等标记的标签（``=A`` / ``≠A``）。它是唯一**跨字位**的一类条件：
    #: 所有带 =A 的字位必须是同一个字，带 ≠A 的字位必须与它不同。
    #: 标签是单个字母，解析时统一大写。
    eq: frozenset[str] = frozenset()
    neq: frozenset[str] = frozenset()

    def is_empty(self) -> bool:
        """无任何条件——等价于「任意一个字」。"""
        return not (
            self.finals or self.initials or self.tones or self.strokes
            or self.structures or self.liushu or self.parts or self.ids
            or self.literals or self.eq or self.neq
        )

    def stroke_set(self) -> frozenset[int] | None:
        """把若干比较式展开成笔画数集合；无条件时返回 None。"""
        if not self.strokes:
            return None
        out: set[int] = set()
        for op, n in self.strokes:
            if op == "eq":
                out.add(n)
            elif op == "le":
                out.update(range(0, n + 1))
            elif op == "ge":
                out.update(range(n, MAX_STROKES + 1))
        return frozenset(out)

    def to_dict(self) -> dict:
        """给前端逐字表单用的结构形（DSL 是给人看的，这个是给表单用的）。"""
        return {
            "eq": sorted(self.eq),
            "neq": sorted(self.neq),
            "finals": sorted(self.finals),
            "initials": ["xx" if i == "" else i for i in sorted(self.initials)],
            "tones": sorted(self.tones),
            "strokes": [{"op": op, "n": n} for op, n in self.strokes],
            "structures": sorted(self.structures),
            "liushu": sorted(self.liushu),
            "parts": sorted(self.parts),
            "ids": sorted(self.ids),
            "literals": sorted(self.literals),
        }

    def to_dsl(self) -> str:
        """反向序列化，供前端表单 <-> 文本框双向同步。无任何条件时输出 ``[]``。"""
        items = [f"={L}" for L in sorted(self.eq)]
        items += [f"≠{L}" for L in sorted(self.neq)]
        items += [f"~{t}" if t != 5 else "~5" for t in sorted(self.tones)]
        items += [_stroke_to_dsl(op, n) for op, n in self.strokes]
        items += sorted(self.structures)
        items += sorted(self.liushu)
        items += [display_final(f) for f in sorted(self.finals)]
        items += ["xx" if i == "" else i for i in sorted(self.initials)]
        items += sorted(self.parts)
        items += sorted(self.ids)
        if self.literals:
            items.append("(" + "".join(sorted(self.literals)) + ")")
        return "[" + ",".join(items) + "]"


def _stroke_to_dsl(op: str, n: int) -> str:
    return f"{n}-" if op == "le" else (f"{n}+" if op == "ge" else str(n))


@dataclass(frozen=True)
class Literal:
    ch: str


@dataclass(frozen=True)
class Slot:
    constraint: CharConstraint


@dataclass(frozen=True)
class AnyChar:
    pass


@dataclass(frozen=True)
class Star:
    pass


Element = Literal | Slot | AnyChar | Star


@dataclass
class ParseIssue:
    text: str          # 无法识别的原文
    offset: int        # 在归一化后的串中的偏移
    reason: str


@dataclass
class Pattern:
    elements: list[Element] = field(default_factory=list)
    ignored: list[ParseIssue] = field(default_factory=list)
    errors: list[ParseIssue] = field(default_factory=list)
    source: str = ""
    normalized: str = ""

    def is_fixed_length(self) -> bool:
        return not any(isinstance(e, Star) for e in self.elements)

    def fixed_length(self) -> int | None:
        if not self.is_fixed_length():
            return None
        return sum(1 for e in self.elements if not isinstance(e, Star))

    def to_dsl(self) -> str:
        out = []
        for el in self.elements:
            if isinstance(el, Literal):
                out.append(el.ch)
            elif isinstance(el, Slot):
                out.append(el.constraint.to_dsl())
            elif isinstance(el, AnyChar):
                out.append("?")
            else:
                out.append("*")
        return "".join(out)


# --------------------------------------------------------------------------
# 单条件分类
# --------------------------------------------------------------------------

def _parse_mark(item: str) -> tuple[str, str] | None:
    """``=A`` / ``≠A`` / ``!=A`` -> ``("eq"|"neq", "A")``；不是标记返回 None。

    标签必须是**单个 ASCII 字母**，统一成大写（``=a`` 与 ``=A`` 是同一个标签）。
    ``!=`` 是给键盘的退路——``≠`` 在不少输入法里不好打；``！＝`` 打出来会先被
    全半角归一变成 ``!=``，白捡。
    """
    if item.startswith("="):
        kind, label = "eq", item[1:]
    elif item.startswith("≠"):
        kind, label = "neq", item[1:]
    elif item.startswith("!="):
        kind, label = "neq", item[2:]
    else:
        return None
    if len(label) != 1 or not label.isascii() or not label.isalpha():
        return None
    return kind, label.upper()


def _classify(item: str) -> tuple[str, object] | None:
    """判断一个条件属于哪一类，返回 (字段名, 值)；无法识别返回 None。

    判定顺序即优先级。IDS 与括号字面必须排在裸写单字之前。
    """
    if not item:
        return None

    # 标记排在最前：`=` / `≠` 打头的串落不进下面任何一类，先判不会抢走谁。
    mark = _parse_mark(item)
    if mark is not None:
        return mark

    if item.startswith("~"):
        body = item[1:]
        if body in _TONE_TOKENS:
            return "tones", _TONE_TOKENS[body]
        return None

    if item.isdigit():
        return "strokes", ("eq", int(item))
    if len(item) >= 2 and item[-1] in "-−—" and item[:-1].isdigit():
        return "strokes", ("le", int(item[:-1]))
    if len(item) >= 2 and item[-1] == "+" and item[:-1].isdigit():
        return "strokes", ("ge", int(item[:-1]))

    if item in STRUCTURES:
        return "structures", item
    if item in LIUSHU_NAMES:
        return "liushu", item

    if item[0] in IDS_OPERATORS:
        return "ids", item

    # 声母先于韵母判：两表交集为空，顺序其实无所谓，但先判短的更自然。
    ini = canonical_initial(item)
    if ini is not None:
        return "initials", ini
    fin = canonical_final(item)
    if fin is not None:
        return "finals", fin

    # 裸写单字部件。语义是「该字的部首是这个字，或该字的拆字里含这个部件」，
    # 所以 [日] 既能命中 明（部首日）也能命中 白（拆作 ⿱丿日）。
    if len(item) == 1 and _is_han(item):
        return "parts", item

    return None


def parse_constraint_group(
    body: str, offset: int, ignored: list[ParseIssue]
) -> CharConstraint:
    """解析 ``[]`` 里的内容（已去掉方括号）。"""
    acc: dict[str, set] = {
        "finals": set(), "initials": set(), "tones": set(),
        "strokes": [], "structures": set(), "liushu": set(),
        "parts": set(), "ids": set(), "literals": set(),
        "eq": set(), "neq": set(),
    }
    # 逗号与空白都是分隔符。空白单独出现时不该变成条件，所以当作分隔比
    # 「删掉再拼」更贴合意图：[a e] 显然是两个条件，而不是无法识别的 "ae"。
    for raw in re.split(r"[,\s]+", body):
        item = raw.strip()
        if not item:
            continue

        # 字面集：(沐露二勾恶感)
        if len(item) >= 2 and item[0] == "(" and item[-1] == ")":
            acc["literals"].update(_parse_literal_group(item[1:-1], offset, ignored))
            continue
        if item[0] == "(" or item[-1] == ")":
            ignored.append(ParseIssue(raw.strip(), offset, "圆括号不配对"))
            continue

        hit = _classify(item)
        if hit is None:
            ignored.append(ParseIssue(raw.strip(), offset, "无法识别"))
            continue
        key, value = hit
        if key == "strokes":
            acc["strokes"].append(value)
        elif key == "literals":
            acc["literals"].update(value)
        else:
            acc[key].add(value)
    return CharConstraint(
        finals=frozenset(acc["finals"]),
        initials=frozenset(acc["initials"]),
        tones=frozenset(acc["tones"]),
        strokes=tuple(acc["strokes"]),
        structures=frozenset(acc["structures"]),
        liushu=frozenset(acc["liushu"]),
        parts=frozenset(acc["parts"]),
        ids=frozenset(acc["ids"]),
        literals=frozenset(acc["literals"]),
        eq=frozenset(acc["eq"]),
        neq=frozenset(acc["neq"]),
    )


def _parse_literal_group(body: str, offset: int, ignored: list[ParseIssue]) -> set[str]:
    """解析 ``(...)``：组内汉字取并集，其余忽略。"""
    out = set()
    for ch in body:
        if _is_han(ch):
            out.add(ch)
        elif not ch.isspace() and ch not in "，,":
            ignored.append(ParseIssue(ch, offset, "字面集内非汉字"))
    return out


# --------------------------------------------------------------------------
# 顶层模式解析
# --------------------------------------------------------------------------

def _validate_marks(
    slots: list[tuple[int, CharConstraint]], errors: list[ParseIssue]
) -> None:
    """校验相等/不等标记。``slots`` 是 (槽起点偏移, 约束) 的序列。

    ``≠A`` **允许**写在 ``=A`` 之前——匹配时由「悬空义务」兜住（见 matcher）。
    所以这里只查两种真正的错：全式压根没有 ``=A``，以及同一字位里 ``=A`` 与
    ``≠A`` 自相矛盾。
    """
    defined: set[str] = set()
    for _, c in slots:
        defined |= c.eq

    for offset, c in slots:
        for label in sorted(c.eq & c.neq):
            errors.append(ParseIssue(
                f"={label},≠{label}", offset,
                f"={label} 与 ≠{label} 不能写在同一字位"))
        for label in sorted(c.neq - c.eq):
            if label not in defined:
                errors.append(ParseIssue(
                    f"≠{label}", offset, f"≠{label} 没有对应的 ={label}"))


def parse_pattern(text: str) -> Pattern:
    """把整条查询解析成模式。整词锚定，支持 ``?`` 与 ``*``。"""
    norm = _normalize_symbols(text)
    ignored: list[ParseIssue] = []
    errors: list[ParseIssue] = []
    elements: list[Element] = []
    slots: list[tuple[int, CharConstraint]] = []      # 供标记校验用

    i = 0
    n = len(norm)
    while i < n:
        ch = norm[i]

        if ch.isspace() or ch == ",":
            i += 1
            continue

        if ch == "\\":
            if i + 1 >= n:
                errors.append(ParseIssue("\\", i, "转义符后无字符"))
                i += 1
            else:
                elements.append(Literal(norm[i + 1]))
                i += 2
            continue

        if ch == "?":
            elements.append(AnyChar())
            i += 1
            continue

        if ch == "*":
            if not elements or not isinstance(elements[-1], Star):
                elements.append(Star())
            i += 1
            continue

        if ch in "[【〔":
            close = min(
                (p for p in (norm.find(c, i + 1) for c in "]】〕") if p >= 0),
                default=-1,
            )
            if close < 0:
                errors.append(ParseIssue(norm[i:], i, "方括号未闭合"))
                break
            group_off = i + 1
            constraint = parse_constraint_group(norm[group_off:close], group_off, ignored)
            elements.append(Slot(constraint))
            slots.append((i, constraint))
            i = close + 1
            continue

        if ch in "(（":
            close = min(
                (p for p in (norm.find(c, i + 1) for c in ")）") if p >= 0),
                default=-1,
            )
            if close < 0:
                errors.append(ParseIssue(norm[i:], i, "圆括号未闭合"))
                break
            lits = _parse_literal_group(norm[i + 1:close], i + 1, ignored)
            if lits:
                elements.append(Slot(CharConstraint(literals=frozenset(lits))))
            i = close + 1
            continue

        if _is_han(ch) or ch.isalnum():
            elements.append(Literal(ch))
            i += 1
            continue

        ignored.append(ParseIssue(ch, i, "无法识别"))
        i += 1

    _validate_marks(slots, errors)
    return Pattern(elements, ignored, errors, text, norm)
