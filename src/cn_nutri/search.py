"""查询主链路：模式解析 -> 扫描 -> 排序 -> 截断。

CLI 与 Web 后端**共用这一个函数**，避免两端行为漂移。网页表单构造的是同一套
``Pattern``（DSL 从文本框来，表单从结构化输入来），所以不存在「两条真相」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .corpus import Corpus
from .matcher import Match, MatchStats
from .query import ParseIssue, Pattern, parse_pattern
from .rank import SORT_MODES, top_matches

__all__ = ["SearchResult", "search", "MAX_RESULTS"]

#: 宽查询可能命中数万条，硬上限；total 仍返回真实总数
MAX_RESULTS = 2000


@dataclass
class SearchResult:
    pattern: Pattern
    matches: list[Match] = field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 50
    sort_mode: str = "freq"
    stats: MatchStats = field(default_factory=MatchStats)
    elapsed_ms: float = 0.0
    truncated: bool = False
    error: str | None = None

    @property
    def ignored(self) -> list[ParseIssue]:
        return self.pattern.ignored

    @property
    def errors(self) -> list[ParseIssue]:
        return self.pattern.errors


def search(
    corpus: Corpus,
    query: str | Pattern,
    limit: int = 50,
    offset: int = 0,
    sort: str = "freq",
    idiom_only: bool = False,
) -> SearchResult:
    t0 = time.perf_counter()
    pattern = query if isinstance(query, Pattern) else parse_pattern(query)
    if sort not in SORT_MODES:
        sort = "freq"

    res = SearchResult(pattern=pattern, offset=offset, limit=limit, sort_mode=sort)
    if pattern.errors:
        res.error = pattern.errors[0].reason
        res.elapsed_ms = (time.perf_counter() - t0) * 1000
        return res

    matcher = corpus.matcher()
    compiled = matcher.compile(pattern)      # 每个查询只编译一次
    hits: list[Match] = []
    match_one = matcher.match_compiled

    # 定长模式直接取对应长度的词桶；含 * 则必须扫全量
    fixed = pattern.fixed_length()
    candidates = (corpus.all_words if fixed is None
                  else corpus.candidate_len(fixed))

    for w in candidates:
        if idiom_only and not w.is_idiom:
            continue
        res.stats.scanned += 1
        m = match_one(compiled, w)
        if m is not None:
            hits.append(m)

    res.stats.excluded_missing = matcher.stats.excluded_missing
    res.stats.missing_chars = matcher.stats.missing_chars

    # total 是真实命中数（不是这一页的），但排序只需要够排到这一页的那几条：
    # 宽查询动辄几万条，全排序是纯浪费。上限仍是 MAX_RESULTS。
    res.total = len(hits)
    res.truncated = res.total > MAX_RESULTS
    want = min(offset + limit, MAX_RESULTS)
    res.matches = top_matches(hits, sort, corpus.strokes_index, want)[offset:]
    res.elapsed_ms = (time.perf_counter() - t0) * 1000
    return res
