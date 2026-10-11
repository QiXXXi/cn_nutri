"""把 data/cn_nutri.db 导出成浏览器端引擎直接可用的紧凑二进制。

    python -I tools/build_client_data.py            # -> src/cn_nutri/web/data/
    python -I tools/build_client_data.py --check    # 写完读回来逐字段核对

为什么要导出
------------
网页版把**查询整个放在客户端**跑：解析 DSL、扫描 37.6 万词、排序，全在浏览器里
完成，服务器只发一次静态文件。收益是**结构性**的：原先每个请求都要占着 GIL 扫
一遍，实测 24 并发时中位延迟从 135 ms 涨到 2.5 s；挪到客户端后服务器就只是发
静态文件。见 README「客户端引擎」。

工程量在这份导出和 web/engine.js 那 800 多行移植，正确性由
tests/test_client_engine.py 逐字段对拍保证——**性能数字等装了 node 实测再写进
README**，这里不先编一个。

二进制布局
----------
Little-endian。先是 32 字节定长头，随后 13 个「u32 长度 + 载荷」的段，顺序固定
（长度前缀让读端能跳过不认识的段，将来加段不必改版本号）：

    段名             内容
    chars.cp         u32 × n_chars      字符表：码位。**下标即身份**，词文本由它拼出
    chars.strokes    u8  × n_chars      笔画，0 = 未知
    chars.struct     u8  × n_chars      结构下标，0xFF = 无
    chars.liushu     u8  × n_chars      六书下标，0xFF = 无
    chars.cfreq      u32 × n_chars      语料字频，sort_aux 由它现算
    chars.radical    u16 × n_chars      部首：部件表下标，0xFFFF = 无
    chars.ids        u16 长度 + blob    拆字串，0xFFFF = 无
    chars.comps      u32 偏移 + u16[]   该字拆出的部件（部件表下标）
    parts.cp         u32 × n_parts      部件表：码位
    syls             3 × n_syls         音节表 (韵母下标 u8, 声母下标 u8, 声调 u8)
    buckets          8 × (max_len+1)    按词长分桶 (起始词号 u32, 词数 u32)
    words            变长               词记录，见下
    defs             u16 长度 + blob    释义，``--no-definitions`` 时整段是「无」

词记录（词长由所在桶给出，故不重复存）：

    freq    u32
    flags   u8      bit0 成语；bit1 读音取自单字表；
                    bit(2+i) = 第 i 套读音是推定读音（只对多字词，i ≤ 4）
    tier    u8
    n_reads u8
    chars   u16 × len
    reads   n_reads 组 u16 × len（音节表下标）

单字词的读音整套都是「这个字本身有哪些读法」（``source = "char"``），一个字
一个位，所以只用 bit1 记一次就够——它可能有 10 套读音（那、和、敦…），逐套
标位放不下，也不该放：这些读音不是推定的，是**并列的歧义**，任一套满足即命中。

省下来的三样
------------
* ``pinyin_display``（4.1 MB）：由 ``format_pinyin`` 现算。没有直接存，是因为它
  是纯函数、词内每个音节都有确定的（声母, 韵母, 调），重建只要一张字表。
* ``sort_aux``（2.9 MB）：``build_db.py`` 里它就是 ``sum(char_freq[c] for c in w)``，
  而 char_freq 能由词表本身复原（对 freq>0 的**多字**词按字累加——单字词要排除，
  两边规则成对），所以只存 chars.cfreq（30 KB）。``--check`` 会逐词验算，
  不是「应该能推出」而是「算出 375804 条全等」。
* 词的文本（1.1 MB）：由 chars 段的码位拼出。

字符表、部件表、音节表都**按使用次数降序**排。词记录里的下标于是绝大多数小于
256，高字节恒为 0，gzip 能把这一半直接压掉。当前实测：含释义 12.98 MB → gzip
5.61 MB，不含释义 8.17 MB → gzip 3.36 MB（``--no-definitions``）。

为什么是这一个文件
------------------
导出与读回都在这里，测试直接 ``import`` 它（见 tests/test_client_data.py）。一个
格式两个实现容易走样，不如让读端就是写端。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import sys
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "src"))

from cn_nutri import pinyin as _pinyin                      # noqa: E402
from cn_nutri import query as _query                        # noqa: E402
from cn_nutri.components import build_components, read_raw   # noqa: E402
from cn_nutri.rank import SORT_MODES                        # noqa: E402
from cn_nutri.search import MAX_RESULTS                     # noqa: E402

# -I 会连 PYTHONIOENCODING 一起忽略，不自己转一次的话在 GBK 控制台上会崩
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

MAGIC = b"CNNUTRI1"
#: 2 起：flags 的 bit1 改义为「读音取自单字表」（v1 里空着）。旧的 engine.js 认得
#: 版本号就会退回服务端，好过把单字词的读音来源静默报成 cedict。
VERSION = 2
DEFAULT_DB = os.path.join(_ROOT, "data", "cn_nutri.db")
DEFAULT_OUT = os.path.join(_ROOT, "src", "cn_nutri", "web", "data")

#: 词 flags 的三个位。与 web/engine.js 里读它们的那几行一一对应，改一处得改两处。
FLAG_IDIOM = 1        # bit0：成语
FLAG_CHAR_READS = 2   # bit1：全部读音都取自单字表（单字词）
FLAG_INFERRED_BASE = 2  # bit(2+i)：第 i 套读音是推定读音

#: 段顺序。名字只用于报错与自检，写读两端都按这个顺序走。
SECTION_NAMES = (
    "chars.cp", "chars.strokes", "chars.struct", "chars.liushu", "chars.cfreq",
    "chars.radical", "chars.ids", "chars.comps", "parts.cp", "syls",
    "buckets", "words", "defs",
)

NULL8 = 0xFF
NULL16 = 0xFFFF


# ---------------------------------------------------------------------------
# 容器
# ---------------------------------------------------------------------------

class Bundle:
    """导出（或读回）的全部数据。纯 Python 容器，方便测试逐字段比对。"""

    def __init__(self) -> None:
        self.max_len = 0
        self.finals: list[str] = []          # 韵母表（内部 ASCII 形）
        self.initials: list[str] = []        # 声母表，第 0 个是零声母 ""
        self.structures: list[str] = []      # 结构名，含汉典里那些不规范的原值
        self.liushu_names: list[str] = []
        self.cps: list[int] = []             # 字符表码位
        self.strokes: list[int] = []         # u8，0 = 未知
        self.struct: list[int] = []          # u8 下标，0xFF = 无
        self.liushu: list[int] = []
        self.cfreq: list[int] = []
        self.radical: list[int] = []         # 部件表下标，0xFFFF = 无
        self.ids: list[str | None] = []
        self.comps: list[list[int]] = []
        self.parts: list[int] = []           # 部件表码位
        self.syls: list[tuple[int, int, int]] = []
        self.buckets: list[tuple[int, int]] = []
        #: (freq, tier, flags, chars, reads, definition)
        self.words: list[tuple] = []

    @property
    def n_chars(self) -> int:
        return len(self.cps)

    @property
    def n_words(self) -> int:
        return len(self.words)


# ---------------------------------------------------------------------------
# db -> Bundle
# ---------------------------------------------------------------------------

def build(db_path: str = DEFAULT_DB, with_definitions: bool = True) -> Bundle:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    b = Bundle()

    char_rows = {r["ch"]: r for r in con.execute("SELECT * FROM chars")}
    word_rows = con.execute(
        "SELECT id, word, freq, tier, is_idiom, definition FROM words "
        "ORDER BY id").fetchall()

    # ---- 语料字频 --------------------------------------------------------
    # build_db.py 里 char_freq 就是对 jieba 词频按字累加，而 jieba 词频就是
    # words.freq（freq=0 的词加不加都一样），所以这里能逐字复原。
    #
    # **单字词必须排除**，而且要和 build_db.py 那条规则成对：那边 char_freq 也
    # 只累加多字词。不排的话 6,857 个单字词会把它们自己的词频（合计 2,600 万）
    # 灌进各自那一格，每个词的 sort_aux 都变，下面 check() 与 JS 端的排序立刻对不上。
    # 理由见 build_db.py「字频」那段：这是为了不让既有 36.8 万条多字词的顺序漂移。
    cfreq: Counter = Counter()
    for r in word_rows:
        if r["freq"] > 0 and len(r["word"]) > 1:
            for ch in r["word"]:
                cfreq[ch] += r["freq"]

    # ---- 字符表：按在语料里出现的次数降序 --------------------------------
    usage: Counter = Counter()
    for r in word_rows:
        usage.update(r["word"])
    order = sorted(char_rows, key=lambda ch: (-usage[ch], ord(ch)))
    cidx = {ch: i for i, ch in enumerate(order)}

    comp_sets = {ch: json.loads(char_rows[ch]["components"]) for ch in order}

    # ---- 部件表 = 所有字拆出部件的并集 ----------------------------------
    # 实测 1569 个，且**已包含全部部首**（部首集合是它的子集），所以部首也存
    # 部件表下标，一个表就够。
    part_usage: Counter = Counter()
    for ch in order:
        part_usage.update(comp_sets[ch])
    part_order = sorted(part_usage, key=lambda c: (-part_usage[c], ord(c)))
    b.parts = [ord(c) for c in part_order]
    pidx = {c: i for i, c in enumerate(part_order)}

    # ---- 结构 / 六书 -----------------------------------------------------
    # 汉典的「字形结构」字段里混着一些没归一干净的原值（「左上包围结构」43 个、
    # 「嵌套结构」5 个…共约 124 个字）。它们在 query.STRUCTURES 里没有对应写法，
    # 于是永远匹配不上——这是**现有行为**，客户端必须照搬：存成独立下标而不是
    # 折成 None，否则这批字会从「确定不匹配」变成「数据缺失」，结果反而不一样。
    b.structures = sorted({char_rows[ch]["structure"] for ch in order
                           if char_rows[ch]["structure"]})
    b.liushu_names = sorted({char_rows[ch]["liushu"] for ch in order
                             if char_rows[ch]["liushu"]})
    sidx = {s: i for i, s in enumerate(b.structures)}
    lidx = {s: i for i, s in enumerate(b.liushu_names)}

    b.cps = [ord(ch) for ch in order]
    b.strokes = [char_rows[ch]["strokes"] or 0 for ch in order]
    b.struct = [sidx.get(char_rows[ch]["structure"], NULL8) for ch in order]
    b.liushu = [lidx.get(char_rows[ch]["liushu"], NULL8) for ch in order]
    b.cfreq = [int(cfreq[ch]) for ch in order]
    b.radical = [pidx[char_rows[ch]["radical"]] if char_rows[ch]["radical"] else NULL16
                 for ch in order]
    b.ids = [char_rows[ch]["ids"] for ch in order]
    b.comps = [[pidx[c] for c in comp_sets[ch]] for ch in order]

    # ---- 音节表：按出现次数降序 ------------------------------------------
    b.finals = sorted(_pinyin.ALL_FINALS)
    b.initials = [""] + list(_pinyin.ALL_INITIALS)
    fin_i = {f: i for i, f in enumerate(b.finals)}
    ini_i = {s: i for i, s in enumerate(b.initials)}

    reads_by_word: dict[int, list[tuple[list[str], str]]] = defaultdict(list)
    syl_usage: Counter = Counter()
    for r in con.execute("SELECT word_id, ridx, syllables, source FROM word_readings "
                         "ORDER BY word_id, ridx"):
        toks = r["syllables"].split()
        reads_by_word[r["word_id"]].append((toks, r["source"]))
        syl_usage.update(toks)

    syl_order = sorted(syl_usage, key=lambda t: (-syl_usage[t], t))
    syl_i = {}
    for t in syl_order:
        ini, fin, tone = t.split("/")
        if fin not in fin_i or ini not in ini_i:
            raise SystemExit(f"词库里出现表外音节 {t!r}——pinyin.py 的表和 DB 不同步")
        syl_i[t] = len(b.syls)
        b.syls.append((fin_i[fin], ini_i[ini], int(tone)))

    # ---- 词表：按词长分桶，桶内保持 DB 的 id 序 --------------------------
    # 定长查询只碰一个桶。桶内顺序不影响结果：四个排序键的末位都是词本身，
    # 而词唯一，所以排序是全序，与候选遍历顺序无关。
    by_len: dict[int, list] = defaultdict(list)
    for r in word_rows:
        by_len[len(r["word"])].append(r)
    b.max_len = max(by_len)

    starts, counts = [], []
    for L in range(b.max_len + 1):
        rows = by_len.get(L, [])
        starts.append(sum(counts))
        counts.append(len(rows))
    b.buckets = list(zip(starts, counts))

    for L in range(b.max_len + 1):
        for r in by_len.get(L, []):
            rl = reads_by_word.get(r["id"], [])
            flags = 0
            if r["is_idiom"]:
                flags |= FLAG_IDIOM
            reads = []
            for i, (toks, source) in enumerate(rl):
                if source == "inferred":
                    if i > 4:
                        raise SystemExit(
                            f"{r['word']!r} 第 {i} 套读音是推定的，flags 位不够"
                            f"（bit(2+i) 只到 bit6）")
                    flags |= 1 << (FLAG_INFERRED_BASE + i)
                elif source == "char":
                    flags |= FLAG_CHAR_READS
                # 词记录里每套读音都是定长 L 的，所以「音节数 ≠ 字数」的读音
                # 根本存不下。实测 0 条（CEDICT 天然 1:1，推定读音也是逐字推的），
                # 但这事不能靠「应该不会发生」——真出现了要当场炸，不能写成定长
                # 数组把后面所有词静默错位。
                if len(toks) != L:
                    raise SystemExit(
                        f"{r['word']!r} 第 {i} 套读音有 {len(toks)} 个音节、"
                        f"词长 {L}——定长格式存不下，需要先改格式")
                reads.append(tuple(syl_i[t] for t in toks))
            definition = (r["definition"] or "") if with_definitions else ""
            b.words.append((
                int(r["freq"]), int(r["tier"]), flags,
                tuple(cidx[c] for c in r["word"]),
                tuple(reads),
                definition,
            ))
    con.close()
    return b


def tables_json(b: Bundle, meta: dict) -> dict:
    """客户端引擎的全部词汇表。词汇表来自 Python 模块本身，JS 只移植逻辑。"""
    return {
        "version": VERSION,
        "pinyin": _pinyin.CLIENT_TABLES,
        "query": _query.CLIENT_TABLES,
        "rank": {"sort_modes": list(SORT_MODES)},
        "search": {"max_results": MAX_RESULTS},
        "corpus": {
            # 音节表里存的是下标，所以这两张表必须和 build() 里用的一模一样
            "finals": b.finals,
            "initials": b.initials,
            "structures": b.structures,
            # 叫 _names 是为了跟字符表里的 u8 下标数组区分开：这张表存的是名字
            "liushu_names": b.liushu_names,
            "n_chars": b.n_chars,
            "n_parts": len(b.parts),
            "n_words": b.n_words,
            "n_syls": len(b.syls),
            "max_len": b.max_len,
        },
        "meta": meta,
    }


def meta_of(db_path: str) -> dict:
    con = sqlite3.connect(db_path)
    out = {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta")}
    con.close()
    return out


# ---------------------------------------------------------------------------
# Bundle -> 字节
# ---------------------------------------------------------------------------

def _p(fmt: str, *vals) -> bytes:
    return struct.pack("<" + fmt, *vals)


def _align4(n: int) -> int:
    return (n + 3) & ~3


def _prefix(data: bytes) -> bytes:
    """u32 「真实长度」 + 载荷 + 补齐到 4 字节。

    长度前缀记的是**补齐前**的长度，但读端要按补齐后的长度前进。段首地址必须
    4 对齐，否则 JS 的 ``Uint32Array`` / ``Uint16Array`` 构造函数会抛 RangeError
    ——不是每个段的长度都恰好是 4 的倍数（``chars.strokes`` 就是 7533 字节）。
    """
    return _p("I", len(data)) + data + b"\0" * (_align4(len(data)) - len(data))


def _blob(strings: list[str | None]) -> bytes:
    """一串变长 UTF-8 字符串：u16 长度表（0xFFFF = 无）+ 连着的字节。"""
    lens, chunks = [], []
    for s in strings:
        if s is None:
            lens.append(NULL16)
        else:
            raw = s.encode("utf-8")
            if len(raw) >= NULL16:
                raise SystemExit("字符串太长，u16 长度装不下")
            lens.append(len(raw))
            chunks.append(raw)
    return _p(f"{len(lens)}H", *lens) + b"".join(chunks)


def _nested(rows: list[list[int]]) -> bytes:
    """偏移表 + 扁平 u16 数组。"""
    flat: list[int] = []
    offs = [0]
    for row in rows:
        flat.extend(row)
        offs.append(len(flat))
    return _p(f"{len(offs)}I", *offs) + _p(f"{len(flat)}H", *flat)


def write_bundle(b: Bundle, path: str) -> int:
    header = (MAGIC + _p("HHIIII", VERSION, 0, b.n_chars, b.n_words,
                         len(b.syls), len(b.parts)) + _p("I", b.max_len))

    words = []
    for freq, tier, flags, chars, reads, _def in b.words:
        L = len(chars)
        rec = [_p("IBBB", freq, flags, tier, len(reads)),
               _p(f"{L}H", *chars)]
        rec.extend(_p(f"{L}H", *r) for r in reads)
        words.append(b"".join(rec))

    flat_buckets = [v for pair in b.buckets for v in pair]
    flat_syls = [v for tri in b.syls for v in tri]

    sections = [
        _p(f"{b.n_chars}I", *b.cps),
        bytes(b.strokes),
        bytes(b.struct),
        bytes(b.liushu),
        _p(f"{b.n_chars}I", *b.cfreq),
        _p(f"{b.n_chars}H", *b.radical),
        _blob(b.ids),
        _nested(b.comps),
        _p(f"{len(b.parts)}I", *b.parts),
        bytes(flat_syls),
        _p(f"{len(flat_buckets)}I", *flat_buckets),
        b"".join(words),
        _defs(b.words),
    ]

    body = b"".join(_prefix(s) for s in sections)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(body)
    return len(header) + len(body)


def _defs(words: list[tuple]) -> bytes:
    """释义用和 ids 同一个编码（u16 长度 + blob）。最长的释义 701 字节，装得下。

    ``--no-definitions`` 时全是空串 → 全写成「无」→ 长度表被 gzip 压成几百字节。
    """
    return _blob([d or None for _f, _t, _fl, _c, _r, d in words])


# ---------------------------------------------------------------------------
# 字节 -> Bundle（读回，给 --check 和测试用）
# ---------------------------------------------------------------------------

def read_bundle(path: str) -> Bundle:
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != MAGIC:
        raise ValueError(f"{path} 不是 cn-nutri 客户端数据（magic 不对）")
    version, _flags, n_chars, n_words, n_syls, n_parts = struct.unpack_from("<HHIIII", data, 8)
    max_len = struct.unpack_from("<I", data, 28)[0]
    if version != VERSION:
        raise ValueError(f"{path} 版本 {version}，本工具只认 {VERSION}")

    pos, payload = 32, {}
    for name in SECTION_NAMES:
        (n,) = struct.unpack_from("<I", data, pos)
        payload[name] = data[pos + 4: pos + 4 + n]
        pos += 4 + _align4(n)
    if pos != len(data):
        raise ValueError("文件尾部有多余字节，段布局对不上")

    b = Bundle()
    b.max_len = max_len
    b.cps = list(struct.unpack(f"<{n_chars}I", payload["chars.cp"]))
    b.strokes = list(payload["chars.strokes"])
    b.struct = list(payload["chars.struct"])
    b.liushu = list(payload["chars.liushu"])
    b.cfreq = list(struct.unpack(f"<{n_chars}I", payload["chars.cfreq"]))
    b.radical = list(struct.unpack(f"<{n_chars}H", payload["chars.radical"]))
    b.ids = _unblob(payload["chars.ids"], n_chars)
    b.comps = _unnested(payload["chars.comps"], n_chars)
    b.parts = list(struct.unpack(f"<{n_parts}I", payload["parts.cp"]))
    flat = payload["syls"]
    b.syls = [tuple(flat[i:i + 3]) for i in range(0, len(flat), 3)]
    assert len(b.syls) == n_syls, "音节表长度对不上"
    flat_b = struct.unpack(f"<{2 * (max_len + 1)}I", payload["buckets"])
    b.buckets = list(zip(flat_b[0::2], flat_b[1::2]))

    defs = _unblob(payload["defs"], n_words)

    words, pos = [], 0
    for L in range(max_len + 1):
        start, count = b.buckets[L]
        assert start == len(words), f"桶 {L} 的起始词号对不上"
        for _ in range(count):
            freq, flags, tier, n_reads = struct.unpack_from("<IBBB", payload["words"], pos)
            pos += 7
            chars = struct.unpack_from(f"<{L}H", payload["words"], pos)
            pos += 2 * L
            reads = []
            for _r in range(n_reads):
                reads.append(struct.unpack_from(f"<{L}H", payload["words"], pos))
                pos += 2 * L
            words.append((freq, tier, flags, tuple(chars), tuple(reads),
                          defs[len(words)] or ""))
    assert len(words) == n_words, f"词数对不上：{len(words)} != {n_words}"
    assert pos == len(payload["words"]), "词段尾部有多余字节"
    b.words = words
    return b


def _unblob(data: bytes, n: int) -> list[str | None]:
    lens = struct.unpack(f"<{n}H", data[:2 * n])
    blob, pos, out = data[2 * n:], 0, []
    for ln in lens:
        if ln == NULL16:
            out.append(None)
        else:
            out.append(blob[pos:pos + ln].decode("utf-8"))
            pos += ln
    return out


def _unnested(data: bytes, n: int) -> list[list[int]]:
    offs = struct.unpack(f"<{n + 1}I", data[:4 * (n + 1)])
    flat = struct.unpack(f"<{offs[-1]}H", data[4 * (n + 1):])
    return [list(flat[offs[i]:offs[i + 1]]) for i in range(n)]


# ---------------------------------------------------------------------------
# 自检：把 Bundle 和 DB 逐字段对一遍
# ---------------------------------------------------------------------------

def check(b: Bundle, db_path: str = DEFAULT_DB, with_definitions: bool = True) -> list[str]:
    """返回不一致之处。空列表 = 导出是无损的（除了刻意省掉的三样）。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    bad: list[str] = []
    fmt = _pinyin.format_pinyin

    char_rows = {r["ch"]: r for r in con.execute("SELECT * FROM chars")}
    if len(char_rows) != b.n_chars:
        bad.append(f"字符数 {b.n_chars} != DB 的 {len(char_rows)}")

    part_ch = [chr(cp) for cp in b.parts]
    word_rows = con.execute(
        "SELECT id, word, freq, tier, is_idiom, definition, pinyin_display "
        "FROM words ORDER BY id").fetchall()
    cp_index = {cp: i for i, cp in enumerate(b.cps)}

    # 字符表的每个字段都对一遍
    for ch, row in char_rows.items():
        i = cp_index.get(ord(ch))
        if i is None:
            bad.append(f"字符 {ch!r} 不在字符表里")
            continue
        if (row["strokes"] or 0) != b.strokes[i]:
            bad.append(f"{ch} 笔画 {b.strokes[i]} != {row['strokes']}")
        if (row["structure"] or None) != (b.structures[b.struct[i]]
                                          if b.struct[i] != NULL8 else None):
            bad.append(f"{ch} 结构对不上")
        if (row["liushu"] or None) != (b.liushu_names[b.liushu[i]]
                                       if b.liushu[i] != NULL8 else None):
            bad.append(f"{ch} 六书对不上")
        if (row["radical"] or None) != (part_ch[b.radical[i]]
                                        if b.radical[i] != NULL16 else None):
            bad.append(f"{ch} 部首对不上")
        if (row["ids"] or None) != b.ids[i]:
            bad.append(f"{ch} 拆字对不上")
        if set(json.loads(row["components"])) != {part_ch[k] for k in b.comps[i]}:
            bad.append(f"{ch} 部件对不上")
        if len(bad) > 20:
            return bad + ["（只列前 20 条）"]

    # 字频自检：复原出来的 char_freq 必须能重现 DB 的 sort_aux（客户端就是靠
    # 这条来省掉 2.9 MB 的浮点数组的）
    cfreq = {chr(cp): b.cfreq[i] for i, cp in enumerate(b.cps)}
    aux_bad = 0
    for r in con.execute("SELECT word, sort_aux FROM words"):
        if abs(float(sum(cfreq.get(c, 0) for c in r["word"])) - r["sort_aux"]) > 1e-9:
            aux_bad += 1
    if aux_bad:
        bad.append(f"{aux_bad} 个词的 sort_aux 无法由 chars.cfreq 复原")

    reads: dict[int, list[tuple[list[str], str]]] = defaultdict(list)
    for r in con.execute("SELECT word_id, ridx, syllables, source FROM word_readings "
                         "ORDER BY word_id, ridx"):
        reads[r["word_id"]].append((r["syllables"].split(), r["source"]))

    if len(word_rows) != b.n_words:
        bad.append(f"词数 {b.n_words} != DB 的 {len(word_rows)}")
        return bad

    # 词表按词长重排过，所以用词文本（唯一）而不是 DB id 来找对应的行
    by_word = {r["word"]: r for r in word_rows}
    for L in range(b.max_len + 1):
        start, count = b.buckets[L]
        for freq, tier, flags, chars, wreads, definition in b.words[start:start + count]:
            want = "".join(chr(b.cps[c]) for c in chars)
            row = by_word.get(want)
            if row is None:
                bad.append(f"词库里没有 {want!r}")
                return bad
            if len(want) != L:
                bad.append(f"{want} 落在 {L} 字桶里，实际 {len(want)} 字")
                return bad
            if (freq, tier, bool(flags & FLAG_IDIOM)) != (row["freq"], row["tier"],
                                                          bool(row["is_idiom"])):
                bad.append(f"{want} 的 freq/tier/成语对不上")
                return bad
            rl = reads.get(row["id"], [])
            if len(rl) != len(wreads):
                bad.append(f"{want} 读音套数 {len(wreads)} != {len(rl)}")
                return bad
            # bit1 是**词级**的（单字词整套读音同源），与逐套的推定位分开判，
            # 与上面写侧的 `elif` 三分支对称。
            if bool(flags & FLAG_CHAR_READS) != any(s == "char" for _, s in rl):
                bad.append(f"{want} 的单字读音标记对不上")
                return bad
            display = []
            for i, ((toks, source), sreads) in enumerate(zip(rl, wreads)):
                if bool(flags & (1 << (FLAG_INFERRED_BASE + i))) != (source == "inferred"):
                    bad.append(f"{want} 第 {i} 套读音的推定标记对不上")
                    return bad
                got_toks = [f"{b.initials[b.syls[s][1]]}/{b.finals[b.syls[s][0]]}/"
                            f"{b.syls[s][2]}" for s in sreads]
                if got_toks != toks:
                    bad.append(f"{want} 第 {i} 套读音 {got_toks} != {toks}")
                    return bad
                display.append(" ".join(
                    fmt(b.initials[b.syls[s][1]], b.finals[b.syls[s][0]], b.syls[s][2])
                    for s in sreads))
            # pinyin_display 是省掉的三样之一：DB 那一列只存了**第一套**读音，
            # 客户端每题都要重建全部套
            if display and row["pinyin_display"] and display[0] != row["pinyin_display"]:
                bad.append(f"{want} 的 pinyin_display 重建不出来："
                           f"{display[0]!r} != {row['pinyin_display']!r}")
                return bad
            if with_definitions and (row["definition"] or "") != definition:
                bad.append(f"{want} 的释义对不上")
                return bad
    return bad


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出客户端引擎用的紧凑词库")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--no-definitions", action="store_true",
                    help="不打包释义（省约 4.7 MB，结果页就没有释义可显示）")
    ap.add_argument("--check", action="store_true",
                    help="写完读回来，和 DB 逐字段核对")
    args = ap.parse_args(argv)

    b = build(args.db, with_definitions=not args.no_definitions)
    bin_path = os.path.join(args.out, "corpus.bin")
    n = write_bundle(b, bin_path)
    # newline="\n"：Windows 的文本模式会把 \n 翻成 \r\n，而仓库里这两个 JSON 按 LF
    # 存（.gitattributes 里写了 *.json text eol=lf）。不指定的话每次重建都会凭空
    # 多出一份「改了行尾」的 diff。
    with open(os.path.join(args.out, "tables.json"), "w", encoding="utf-8",
              newline="\n") as fh:
        json.dump(tables_json(b, meta_of(args.db)), fh,
                  ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")

    # 部件面板也一并烘出来：仓库不带 data_raw/，线上服务读不到字统网那份原始表，
    # 没有这个文件页面上的部件面板就是空的。
    comp = build_components(read_raw())
    comp_path = os.path.join(args.out, "components.json")
    with open(comp_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(comp, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")

    back = read_bundle(bin_path)
    if (back.n_words, back.n_chars, back.max_len) != (b.n_words, b.n_chars, b.max_len):
        print("读回来对不上，格式有 bug", file=sys.stderr)
        return 1

    tables_path = os.path.join(args.out, "tables.json")
    print(f"{b.n_words} 词 / {b.n_chars} 字 / {len(b.parts)} 部件 / "
          f"{len(b.syls)} 音节 / 词长 ≤ {b.max_len}")
    print(f"  {bin_path}   {n/1e6:.2f} MB 原始 / {_gzip_size(bin_path)/1e6:.2f} MB gzip")
    print(f"  {tables_path}   {os.path.getsize(tables_path)/1024:.1f} KB")
    print(f"  {comp_path}   {len(comp['groups'])} 组 / "
          f"{len({c for g in comp['groups'] for c in g['chars']})} 个部件")

    if args.check:
        bad = check(b, args.db, with_definitions=not args.no_definitions)
        if bad:
            print(f"自检发现 {len(bad)} 处不一致：", file=sys.stderr)
            for line in bad:
                print("  " + line, file=sys.stderr)
            return 1
        print("自检通过：字符表、词表、读音、字频（sort_aux）逐条与 DB 一致")
    return 0


def _gzip_size(path: str, level: int = 6) -> int:
    import gzip
    with open(path, "rb") as fh:
        return len(gzip.compress(fh.read(), level))


if __name__ == "__main__":
    raise SystemExit(main())
