"""排序。

词频（jieba）覆盖不全，所以主排序是**三级**：

- **Tier 0** ``freq > 0``      → 按词频降序
- **Tier 1** ``freq == 0`` 但在 CC-CEDICT → 按字频和降序
- **Tier 2** jieba 独有        → 按字频和降序

Tier 1 排在 Tier 2 之前，是因为 CEDICT 收录的是经过人工甄别的真词，
不该被 jieba 里那些切分歧义产生的低频串埋掉。零频词用「字频和」兜底，
让它们也有一个可解释的次序，而不是随机。
"""

from __future__ import annotations

import heapq
from typing import Callable, Iterable

from .matcher import Match, Word

__all__ = ["SORT_MODES", "sort_matches", "sort_key", "top_matches"]

SORT_MODES = ("freq", "len", "strokes", "pinyin")


def sort_key(w: Word) -> tuple:
    """默认序：层级 -> 词频 -> 字频和。"""
    return (w.tier, -w.freq, -w.sort_aux, w.word)


def _len_key(m: Match) -> tuple:
    return (m.word.tier, len(m.word.word), -m.word.freq, -m.word.sort_aux, m.word.word)


def _strokes_key(m: Match, char_strokes: dict[str, int | None]) -> tuple:
    total = sum(char_strokes.get(c) or 0 for c in m.word.word)
    return (total, m.word.tier, -m.word.freq, m.word.word)


def _pinyin_key(m: Match) -> tuple:
    if 0 <= m.read_index < len(m.word.pinyin_display):
        py = m.word.pinyin_display[m.read_index]
    else:
        py = ""
    return (py, m.word.tier, -m.word.freq, m.word.word)


def _keyfunc(mode: str,
             char_strokes: dict[str, int | None] | None) -> Callable[[Match], tuple]:
    if mode == "len":
        return _len_key
    if mode == "strokes":
        cs = char_strokes or {}
        return lambda m: _strokes_key(m, cs)
    if mode == "pinyin":
        return _pinyin_key
    return _freq_key


def _freq_key(m: Match) -> tuple:
    return (m.word.tier, -m.word.freq, -m.word.sort_aux, m.word.word)


def sort_matches(
    matches: Iterable[Match],
    mode: str = "freq",
    char_strokes: dict[str, int | None] | None = None,
) -> list[Match]:
    return sorted(matches, key=_keyfunc(mode, char_strokes))


def top_matches(
    matches: Iterable[Match],
    mode: str = "freq",
    char_strokes: dict[str, int | None] | None = None,
    k: int | None = None,
) -> list[Match]:
    """只要前 ``k`` 条时用堆选，别为了一页结果把几万条全排一遍。

    排序键末位是词本身，而词唯一，所以键是全序——``nsmallest`` 的前 k 条与
    ``sorted`` 的前 k 条逐条相同，不是近似。
    """
    key = _keyfunc(mode, char_strokes)
    if k is None:
        return sorted(matches, key=key)
    return heapq.nsmallest(k, matches, key=key)
