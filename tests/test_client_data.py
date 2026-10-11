"""客户端词库的二进制格式：写出去、读回来，必须和 DB 逐字段一致。

不需要 node，也不用真跑一遍 build_client_data.py 的命令行——直接调
build() / write_bundle() / read_bundle() / check()。耗时的是 build()（约 10 秒），
所以整个模块只建一次。

这里守的是**格式**，不是引擎。引擎那条在 tests/test_client_engine.py。
"""

from __future__ import annotations

import os
import struct
import sys
from collections import Counter

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "cn_nutri.db")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_client_data as bcd  # noqa: E402

if not os.path.exists(DB):
    pytest.skip("没有 data/cn_nutri.db：先跑 python -I tools/build_db.py",
                allow_module_level=True)


@pytest.fixture(scope="module")
def built():
    return bcd.build(DB)


@pytest.fixture(scope="module")
def written(built, tmp_path_factory):
    path = str(tmp_path_factory.mktemp("client") / "corpus.bin")
    bcd.write_bundle(built, path)
    return path


# ------------------------------------------------------------------ 往返

def test_roundtrip_is_lossless(built, written):
    back = bcd.read_bundle(written)

    assert back.n_chars == built.n_chars
    assert back.n_words == built.n_words
    assert back.max_len == built.max_len
    assert back.cps == built.cps
    assert back.strokes == built.strokes
    assert back.struct == built.struct
    assert back.liushu == built.liushu
    assert back.cfreq == built.cfreq
    assert back.radical == built.radical
    assert back.ids == built.ids
    assert back.comps == built.comps
    assert back.parts == built.parts
    assert back.syls == built.syls
    assert back.buckets == built.buckets
    assert back.words == built.words

    # 词汇表（韵母名、声母名、结构名、六书名）**不进**二进制，它们只在
    # tables.json 里。所以这几项读回来必须是空的——哪天有人顺手把它们也塞进
    # 文件里，这条会提醒他改的是格式。
    assert back.finals == [] and back.initials == []
    assert back.structures == [] and back.liushu_names == []


def test_sections_are_four_byte_aligned(written):
    """段首必须 4 字节对齐，否则 JS 的 Uint32Array 直接抛 RangeError。

    段长度不总是 4 的倍数（chars.strokes 就是 7533 字节），所以写端得补零。
    """
    data = open(written, "rb").read()
    pos = 32
    for name in bcd.SECTION_NAMES:
        assert pos % 4 == 0, f"{name} 段起点 {pos} 没对齐到 4 字节"
        (n,) = struct.unpack_from("<I", data, pos)
        pos += 4 + bcd._align4(n)
    assert pos == len(data)


def test_definitions_can_be_dropped(tmp_path):
    """--no-definitions 要能把 4.5 MB 的释义整段去掉，而不是留一堆空串。"""
    b = bcd.build(DB, with_definitions=False)
    assert all(w[5] == "" for w in b.words)
    full = str(tmp_path / "full.bin")
    thin = str(tmp_path / "thin.bin")
    bcd.write_bundle(bcd.build(DB, with_definitions=True), full)
    bcd.write_bundle(b, thin)
    assert os.path.getsize(thin) < os.path.getsize(full) - 4_000_000


# ------------------------------------------------------------------ 与 DB 对拍

def test_check_finds_no_mismatch(built):
    """check() 会验：字符表每个字段、每套读音、以及据此**现算**的 sort_aux
    和 pinyin_display。后者是省掉那 7 MB 的前提，不能只靠「应该能推出来」。"""
    assert bcd.check(built, DB) == []


def test_char_freq_excludes_single_char_words(built):
    """语料字频**不**累加单字词自己的词频。

    客户端不存 sort_aux（省 2.9 MB），是靠 chars.cfreq 现算的；而 build_db.py 那边
    也用的是同一个数。两边都必须排除单字词——不然 6,857 个单字词会把自己的词频灌进
    各自那一格，既有 36.8 万条多字词的 tie-break 顺序整体漂移。这是**成对**规则，所以
    「只算多字词」得到的表必须相同，而「连单字词也算」得到的表必须**不同**：
    只断言前一半的话，哪天两边一起改错，这条照样绿。
    """
    import sqlite3
    con = sqlite3.connect(DB)
    with_single: Counter = Counter()
    without_single: Counter = Counter()
    for w, f in con.execute("SELECT word, freq FROM words"):
        if f <= 0:
            continue
        for c in w:
            with_single[c] += f
            if len(w) > 1:
                without_single[c] += f
    got = {chr(cp): built.cfreq[i] for i, cp in enumerate(built.cps)}
    assert got == {c: without_single[c] for c in got}
    assert got != {c: with_single[c] for c in got}, "单字词频被算进字频表了"


def test_check_notices_a_tampered_bundle(built):
    """反向验证：动了数据就得报错，否则上面那条「全过」是假绿。"""
    import copy
    broken = copy.copy(built)
    broken.cps = list(built.cps)
    broken.cps[0] = 0x41          # 把第一个字改成 'A'
    assert bcd.check(broken, DB)


# ------------------------------------------------------------------ tables.json

def test_tables_json_carries_every_vocabulary(built):
    t = bcd.tables_json(built, bcd.meta_of(DB))
    for key in ("finals", "initials", "structures", "liushu_names", "max_len"):
        assert key in t["corpus"], key
    for key in ("finals", "initials", "tone_mark_table", "tone_marks",
                "final_aliases", "display_final", "zero_form", "parse_prefixes"):
        assert key in t["pinyin"], key
    for key in ("structures", "liushu", "ids_operators", "han_ranges",
                "punct_map", "tone_tokens", "max_strokes"):
        assert key in t["query"], key

    # 音节表存的是下标，这三张表必须一一对上
    assert len(t["corpus"]["initials"]) == 1 + len(t["pinyin"]["initials"])
    assert t["corpus"]["initials"][0] == ""
    assert t["corpus"]["finals"] == t["pinyin"]["finals"]


def test_char_table_covers_every_word_char(built):
    known = {chr(cp) for cp in built.cps}
    import sqlite3
    con = sqlite3.connect(DB)
    missing = set()
    for (word,) in con.execute("SELECT word FROM words"):
        missing |= {c for c in word if c not in known}
    assert not missing, f"{len(missing)} 个字不在字符表里：{''.join(sorted(missing))[:40]}"


def test_char_table_is_ordered_by_frequency(built):
    """字符表按使用次数降序排，词记录里的下标才大多小于 256、高字节恒为 0，
    gzip 才压得动。排错了不会算错结果，但文件会大一截——所以要守着。"""
    import sqlite3
    con = sqlite3.connect(DB)
    ts = {}
    for w, f in con.execute("SELECT word, freq FROM words"):
        for c in w:
            ts[c] = ts.get(c, 0) + 1
    counts = [ts.get(chr(cp), 0) for cp in built.cps]
    assert counts == sorted(counts, reverse=True)
