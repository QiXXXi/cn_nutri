"""约束模型 × 词库 -> 命中。

匹配分两层：
1. **结构层**：模式元素（字面 / 约束槽 / ? / *）与词的位置做通配匹配。定长模式走
   一条直路循环；含 ``*`` 才退化成带回溯的 glob（记忆化搜索，词长 ≤ 12 所以代价
   可忽略）。
2. **属性层**：落在一个约束槽上的字，逐类比对它的笔画/结构/部件/该语境下的读音。

多音字按**词在该语境下的读音**判定（银行 yínháng ≠ 行走 xíngzòu）：一套读音里
每个位置都有确定的音节。一词多读时任一套满足即命中，并记下是哪一套以便高亮。

数据缺失是**显式**的：某字的某个属性没抓到，该字就无法判定，于是**含这个字的词
判为不命中**，并计入 ``MatchStats``（结果页提示「N 个词因数据缺失被排除」）。
不放行是因为放行等于把「不知道」当成「满足」——那才是静默答错。

性能
----
约束里「每个字都要重算」的部分（尤其 ``stroke_set()`` 会把笔画条件展开成 0..64
的集合）在 ``_prepare`` 里每类只算一次，扫描时用预编译元组，避免递归与重复分配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple, Sequence

from .pinyin import Syllable
from .query import AnyChar, CharConstraint, Element, Literal, Pattern, Slot, Star

__all__ = ["CharInfo", "Word", "Match", "MatchStats", "Matcher", "match_char"]

#: 部件约束的匹配深度上限——IDS 递推展开到第几层
MAX_COMPONENT_DEPTH = 4


class CharInfo(NamedTuple):
    ch: str
    strokes: int | None
    radical: str | None
    structure: str | None
    liushu: str | None
    ids: str | None
    components: frozenset[str]
    readings: tuple[Syllable, ...] = ()


class Word(NamedTuple):
    word: str
    reads: tuple[tuple[Syllable, ...], ...]   # 多套读音，每套逐音节
    read_sources: tuple[str, ...]             # cedict | inferred
    freq: int
    tier: int
    sort_aux: float
    pinyin_display: tuple[str, ...]
    is_idiom: bool
    definition: str

    @property
    def has_pinyin(self) -> bool:
        return bool(self.reads)


@dataclass
class Match:
    word: Word
    read_index: int
    spans: list[int] = field(default_factory=list)   # 每个非 * 元素命中的字位


@dataclass
class MatchStats:
    scanned: int = 0
    #: 因为缺数据而无法判定、从而被排除的词数
    excluded_missing: int = 0
    #: 这些词里究竟缺了哪些字
    missing_chars: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------
# 预编译：把一个字的约束压成扁平元组，扫描时不再碰 dataclass 属性
# --------------------------------------------------------------------------

#: ``_prepare`` 末尾两项是相等/不等标记。散落着用 ``payload[10]`` 太脆，起个名。
_P_EQ, _P_NEQ = 10, 11


def _prepare(c: CharConstraint) -> tuple:
    """``stroke_set()`` 会展开成 0..64 的集合，原本按字重算，是扫描里最贵的一步。"""
    return (
        c.is_empty(), c.literals, c.initials, c.finals, c.tones,
        c.stroke_set(), c.structures, c.liushu, c.parts, c.ids,
        c.eq, c.neq,
    )


def _apply_marks(
    payload: tuple, word: str, wi: int, bind: tuple, oblig: tuple
) -> tuple[bool, tuple, tuple]:
    """把一个 ``[]`` 的相等/不等标记并入游标状态。

    词是**给定的**，所以这里比的全是已经确定的字，不需要搜索。唯一麻烦的是
    ``≠A`` 可能写在 ``=A`` 之前——那会儿还不知道 A 绑到哪个字位，于是先把
    ``(A, 字位)`` 记成一条**悬空义务**，等后面某个字位绑上 A 时回头兑现。

    ``bind`` / ``oblig`` 都是不可变元组（可哈希，能直接进 ``_glob`` 的 memo key），
    回溯时天然不用撤销。没有标记的模式下两者恒为空元组，热路径零额外开销。

    返回 ``(是否满足, 新 bind, 新 oblig)``。
    """
    eq_labels, neq_labels = payload[_P_EQ], payload[_P_NEQ]
    if not (eq_labels or neq_labels):
        return True, bind, oblig

    ch = word[wi]
    merged = dict(bind)
    for label in eq_labels:
        prev = merged.get(label)
        if prev is not None and prev != ch:
            return False, bind, oblig
        merged[label] = ch

    # 刚刚绑上的标签，可能正欠着前面某个 ≠ 的义务
    for label, oi in oblig:
        if label in merged and word[oi] == merged[label]:
            return False, bind, oblig

    for label in neq_labels:
        if label in merged:
            if ch == merged[label]:
                return False, bind, oblig
        else:
            oblig = oblig + ((label, wi),)

    return True, tuple(sorted(merged.items())), tuple(sorted(oblig))


def match_prepared(p: tuple, ch: str, syl: Syllable | None,
                   info: CharInfo | None) -> bool | None:
    """``match_char`` 的预编译版。True/False 为判定，None 表示数据缺失。"""
    (empty, literals, initials, finals, tones,
     strokes, structures, liushu, parts, ids, _eq, _neq) = p
    if empty:
        return True
    if literals and ch not in literals:
        return False

    if initials or finals or tones:
        if syl is None:
            return None
        if initials and syl.initial not in initials:
            return False
        if finals and syl.final not in finals:
            return False
        if tones and syl.tone not in tones:
            return False

    if info is None:
        # 无字形数据：只要还有字形类条件就无法判定
        if strokes is not None or structures or liushu or parts or ids:
            return None
        return True

    if strokes is not None:
        if info.strokes is None:
            return None
        if info.strokes not in strokes:
            return False
    if structures:
        if info.structure is None:
            return None
        if info.structure not in structures:
            return False
    if liushu:
        if info.liushu is None:
            return None
        if info.liushu not in liushu:
            return False
    if ids and (info.ids is None or info.ids not in ids):
        return False

    # 部件语义是并集：以该字为部首，或该字的拆字里含这个部件。
    # 所以 [日] 同时命中 明（部首日）与 白（拆作 ⿱丿日）。
    if parts and not (info.radical in parts or (parts & info.components)):
        return False

    return True


def match_char(c: CharConstraint, ch: str, syl: Syllable | None,
               info: CharInfo | None) -> bool | None:
    """一个字是否满足约束。None 表示因数据缺失而无法判定。"""
    return match_prepared(_prepare(c), ch, syl, info)


#: 编译后的模式元素：("lit", 字) / ("slot", 预编译约束) / ("any", None)
Compiled = NamedTuple("Compiled", [("elements", list), ("has_star", bool)])


class Matcher:
    """持有字形索引，对模式做全量扫描。"""

    def __init__(self, chars: dict[str, CharInfo]) -> None:
        self.chars = chars
        self.stats = MatchStats()
        self._scratch: set[str] = set()

    # ---- 编译 -----------------------------------------------------------
    def compile(self, pattern: Pattern) -> Compiled:
        """把模式的每个元素换成扫描时的紧凑表示。每个查询只做一次。"""
        els: list[tuple[str, object]] = []
        has_star = False
        for el in pattern.elements:
            if isinstance(el, Slot):
                els.append(("slot", _prepare(el.constraint)))
            elif isinstance(el, Literal):
                els.append(("lit", el.ch))
            elif isinstance(el, AnyChar):
                els.append(("any", None))
            else:
                has_star = True
                els.append(("star", None))
        return Compiled(els, has_star)

    # ---- 结构层 ---------------------------------------------------------
    def _scan(self, els: Sequence[tuple], word: str, reading,
              unknown: set[str]) -> list[int] | None:
        """定长直路：无 ``*`` 时不需要回溯。"""
        chars = self.chars
        spans = []
        bind: tuple = ()
        oblig: tuple = ()
        for i, (kind, payload) in enumerate(els):
            ch = word[i]
            if kind == "slot":
                # 标记先判：它冲突是**确定**的不匹配，不该被记成「数据缺失」
                ok, bind, oblig = _apply_marks(payload, word, i, bind, oblig)
                if not ok:
                    return None
                syl = reading[i] if reading is not None else None
                verdict = match_prepared(payload, ch, syl, chars.get(ch))
                if verdict is None:
                    unknown.add(ch)
                    return None
                if not verdict:
                    return None
            elif kind == "lit" and ch != payload:
                return None
            spans.append(i)
        return spans

    def _glob(self, els: Sequence[tuple], word: str, reading,
              unknown: set[str]) -> list[int] | None:
        """含 ``*`` 时的记忆化回溯。成功返回每个非 ``*`` 元素命中的字位。"""
        n = len(els)
        m = len(word)
        chars = self.chars
        # 无标记时 bind/oblig 恒为 ()，键退化回 (ei, wi)，与从前等价
        memo: dict[tuple, list[int] | None] = {}

        def rec(ei: int, wi: int, bind: tuple, oblig: tuple) -> list[int] | None:
            key = (ei, wi, bind, oblig)
            if key in memo:
                return memo[key]
            if ei == n:
                res: list[int] | None = [] if wi == m else None
                memo[key] = res
                return res

            kind, payload = els[ei]
            out: list[int] | None = None

            if kind == "star":
                for k in range(wi, m + 1):
                    tail = rec(ei + 1, k, bind, oblig)
                    if tail is not None:
                        out = tail
                        break
            elif wi >= m:
                out = None
            elif kind == "any":
                tail = rec(ei + 1, wi + 1, bind, oblig)
                if tail is not None:
                    out = [wi] + tail
            elif kind == "lit":
                if word[wi] == payload:
                    tail = rec(ei + 1, wi + 1, bind, oblig)
                    if tail is not None:
                        out = [wi] + tail
            else:
                ch = word[wi]
                ok, nbind, noblig = _apply_marks(payload, word, wi, bind, oblig)
                if ok:
                    syl = reading[wi] if reading is not None else None
                    verdict = match_prepared(payload, ch, syl, chars.get(ch))
                    if verdict is None:
                        unknown.add(ch)
                    elif verdict:
                        tail = rec(ei + 1, wi + 1, nbind, noblig)
                        if tail is not None:
                            out = [wi] + tail

            memo[key] = out
            return out

        return rec(0, 0, (), ())

    # ---- 对外 -----------------------------------------------------------
    def match_compiled(self, compiled: Compiled, w: Word,
                       missing_chars: set[str] | None = None) -> Match | None:
        """一个词是否命中；命中则给出命中的读音序号与字位。"""
        word = w.word
        n = len(word)
        reads = w.reads
        # 逐音节读音必须与词长一一对应。不匹配说明数据有问题，宁可判不中，
        # 也不能让缺失的音节被当成「任意」而静默放行。
        # 99.99% 的词只有一套读音且天然对齐，所以先给这条路留个 O(1) 出口。
        if reads:
            if len(reads) == 1:
                if len(reads[0]) != n:
                    return None
            elif not all(len(r) == n for r in reads):
                reads = tuple(r for r in reads if len(r) == n)
                if not reads:
                    return None

        els = compiled.elements
        if not compiled.has_star and len(els) != n:
            return None

        unknown = self._scratch
        unknown.clear()
        run = self._glob if compiled.has_star else self._scan

        for ri, reading in enumerate(reads or (None,)):
            spans = run(els, word, reading, unknown)
            if spans is not None:
                return Match(w, ri if reads else -1, spans)

        if unknown:
            # 这个词不是「不满足」，而是「判不了」——分开计数，好在结果页说明
            self.stats.excluded_missing += 1
            self.stats.missing_chars |= unknown
            if missing_chars is not None:
                missing_chars |= unknown
            unknown.clear()
        return None

    def match(self, pattern: Pattern, w: Word,
              missing_chars: set[str] | None = None) -> Match | None:
        """便利入口：每次调用都重新编译。批量扫描请用 ``match_compiled``。"""
        return self.match_compiled(self.compile(pattern), w, missing_chars)
