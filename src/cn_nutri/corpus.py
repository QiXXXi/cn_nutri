"""把 SQLite 构建产物整份载入内存。

为什么不建逐字属性索引表：全量 37.6 万词暴力扫描是**秒级**，而「12 ms」那个数
（定方案时测的）在实现后没能复现——见 README「性能」。这一版靠预编译约束和免回溯
直路把定长查询压到 100–200 ms，索引没做。构建产物是**构建产物与展示存储**，
不是查询引擎——查询路径完全不碰 SQLite。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Iterable

from .matcher import CharInfo, Matcher, Word
from .pinyin import Syllable

__all__ = ["Corpus", "DEFAULT_DB", "load_corpus"]

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "cn_nutri.db",
)


def _parse_reading(raw: str) -> tuple[Syllable, ...]:
    """``"d/an/1 ao/ao/4"`` -> (Syllable('d','an',1), Syllable('ao','ao',4))"""
    out = []
    for tok in raw.split():
        ini, fin, tone = tok.split("/")
        out.append(Syllable(ini, fin, int(tone)))
    return tuple(out)


class Corpus:
    def __init__(self, db_path: str = DEFAULT_DB) -> None:
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"找不到 {db_path}——先跑 python -I tools/build_db.py")
        self.db_path = db_path
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row

        self.meta = {r["key"]: r["value"] for r in con.execute("SELECT * FROM meta")}

        self.chars: dict[str, CharInfo] = {}
        for r in con.execute("SELECT * FROM chars"):
            readings = tuple(Syllable(*t) for t in json.loads(r["readings"]))
            self.chars[r["ch"]] = CharInfo(
                ch=r["ch"],
                strokes=r["strokes"],
                radical=r["radical"],
                structure=r["structure"],
                liushu=r["liushu"],
                ids=r["ids"],
                components=frozenset(json.loads(r["components"])),
                readings=readings,
            )

        reads: dict[int, list[tuple[tuple[Syllable, ...], str]]] = {}
        for r in con.execute(
                "SELECT word_id, ridx, syllables, source FROM word_readings "
                "ORDER BY word_id, ridx"):
            reads.setdefault(r["word_id"], []).append(
                (_parse_reading(r["syllables"]), r["source"]))

        self.by_len: dict[int, list[Word]] = {}
        self.all_words: list[Word] = []
        for r in con.execute("SELECT * FROM words ORDER BY id"):
            rl = reads.get(r["id"], [])
            w = Word(
                word=r["word"],
                reads=tuple(x[0] for x in rl),
                read_sources=tuple(x[1] for x in rl),
                freq=r["freq"],
                tier=r["tier"],
                sort_aux=r["sort_aux"],
                pinyin_display=tuple(
                    " ".join(  # 每套读音的展示形，顺序与 reads 一致
                        _fmt(s) for s in x[0]) for x in rl),
                is_idiom=bool(r["is_idiom"]),
                definition=r["definition"] or "",
            )
            self.by_len.setdefault(len(w.word), []).append(w)
            self.all_words.append(w)
        con.close()

        self.strokes_index = {c: i.strokes for c, i in self.chars.items()}

    def matcher(self) -> Matcher:
        return Matcher(self.chars)

    def candidate_len(self, length: int | None) -> Iterable[Word]:
        if length is None:
            return self.all_words
        return self.by_len.get(length, ())

    def __repr__(self) -> str:
        return (f"<Corpus {len(self.all_words)} 词 / {len(self.chars)} 字 "
                f"@ {os.path.basename(self.db_path)}>")


def _fmt(s: Syllable) -> str:
    from .pinyin import format_pinyin
    return format_pinyin(s.initial, s.final, s.tone)


_CACHE: dict[str, Corpus] = {}


def load_corpus(db_path: str = DEFAULT_DB) -> Corpus:
    """进程内缓存：一次性载入约 1 秒，重复查询无需重建。"""
    key = os.path.abspath(db_path)
    if key not in _CACHE:
        _CACHE[key] = Corpus(key)
    return _CACHE[key]
