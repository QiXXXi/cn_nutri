#!/usr/bin/env python3
"""把 data_raw/ 下的各源数据合成 data/cn_nutri.db。

数据来源与分工
--------------
    笔画/部首序号   Unihan kTotalStrokes / kRSUnicode
    结构/六书/部首  汉典（zdic.net）——IDS 推不出「独体字」，必须用汉典
    IDS 拆字        字统网（zi.tools）——仅供部件检索，不参与结构判定
    单字读音        pinyin-data pinyin.txt
    词 + 词级拼音    CC-CEDICT
    词频 + 词性      jieba dict.txt

用 ``python -I`` 运行；原始数据视为不可信输入，只读不执行。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cn_nutri.pinyin import Syllable, format_pinyin, parse_syllable  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data_raw")
DB_PATH = os.path.join(ROOT, "data", "cn_nutri.db")

IDS_OPERATORS = frozenset("⿰⿱⿲⿳⿴⿵⿶⿷⿸⿹⿺⿻⿼⿽⿾⿿")
MAX_COMPONENT_DEPTH = 4
MIN_LEN, MAX_LEN = 2, 12

# 康熙部首 1..214
KANGXI = (
    "一丨丶丿乙亅二亠人儿入八冂冖冫几凵刀力勹匕匚匸十卜卩厂厶又口囗土士夂夊夕大女子宀"
    "寸小尢尸屮山巛工己巾干幺广廴廾弋弓彐彡彳心戈戶手支攴文斗斤方无日曰月木欠止歹殳毋"
    "比毛氏气水火爪父爻爿片牙牛犬玄玉瓜瓦甘生用田疋疒癶白皮皿目矛矢石示禸禾穴立竹米糸"
    "缶网羊羽老而耒耳聿肉臣自至臼舌舛舟艮色艸虍虫血行衣襾見角言谷豆豕豸貝赤走足身車辛"
    "辰辵邑酉釆里金長門阜隶隹雨青非面革韋韭音頁風飛食首香馬骨高髟鬥鬯鬲鬼魚鳥鹵鹿麥麻"
    "黃黍黑黹黽鼎鼓鼠鼻齊齒龍龜龠"
)
assert len(KANGXI) == 214, len(KANGXI)


def is_han(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x2A6DF
        or 0x2A700 <= cp <= 0x2EBEF or 0x2F800 <= cp <= 0x2FA1F
        or 0x30000 <= cp <= 0x323AF
    )


def sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def split_syllables(token: str) -> list[str]:
    """按数字调边界切开。CEDICT 偶尔把两个音节粘在一起（`shi2ke4`）。"""
    out, cur = [], ""
    for ch in token:
        cur += ch
        if ch.isdigit():
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------
# 载入各源
# --------------------------------------------------------------------------

def load_unihan() -> tuple[dict[str, int], dict[str, int]]:
    strokes: dict[str, int] = {}
    radical_no: dict[str, int] = {}
    path = os.path.join(RAW, "unihan", "Unihan_IRGSources.txt")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            cp, field, value = parts[0], parts[1], parts[2]
            ch = chr(int(cp[2:], 16))
            if field == "kTotalStrokes":
                # 极少数字是 "5 5"（简繁同），取第一个
                first = value.split()[0]
                if first.isdigit():
                    strokes[ch] = int(first)
            elif field == "kRSUnicode":
                # 形如 "1.4" / "120'.5"——撇号表示该部首的简化变体
                m = re.match(r"(\d+)", value)
                if m:
                    radical_no[ch] = int(m.group(1))
    return strokes, radical_no


def load_pinyin_data() -> dict[str, tuple[Syllable, ...]]:
    """pinyin-data 的 pinyin.txt：``U+4E00: yī,yi1  # 一``"""
    out: dict[str, tuple[Syllable, ...]] = {}
    path = os.path.join(RAW, "pinyin_data", "pinyin-data-master", "pinyin.txt")
    for line in open(path, encoding="utf-8"):
        if line.startswith("#") or ":" not in line:
            continue
        head, _, rest = line.partition(":")
        comment = rest.find("#")
        body = rest[:comment] if comment >= 0 else rest
        try:
            ch = chr(int(head.strip()[2:], 16))
        except ValueError:
            continue
        syls, seen = [], set()
        for tok in body.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                s = parse_syllable(tok)
            except ValueError:
                continue
            if s not in seen:
                seen.add(s)
                syls.append(s)
        if syls:
            out[ch] = tuple(syls)
    return out


def load_zdic() -> dict[str, dict]:
    out: dict[str, dict] = {}
    d = os.path.join(RAW, "zdic")
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(d, name), encoding="utf-8") as fh:
            rec = json.load(fh)
        out[rec["char"]] = rec
    return out


def load_zitools() -> tuple[dict[str, str], dict]:
    ids: dict[str, str] = {}
    d = os.path.join(RAW, "zitools")
    difficult: dict = {}
    if os.path.isdir(d):
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            path = os.path.join(d, name)
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
            if name == "_difficult_components.json":
                difficult = rec
            elif rec.get("ids"):
                ids[rec["char"]] = rec["ids"]
    return ids, difficult


def load_makemeahanzi() -> dict[str, str]:
    """本地 makemeahanzi 的 ``decomposition``，作为字统网 IDS 的兜底。

    zi.tools 对基本区汉字的单次响应要 5～20 秒（服务端按需计算），全量 7,533 字
    在 6 并发下仍需数小时；makemeahanzi 本地就有 9,574 字的拆字，覆盖语料的
    6,728 字。按「字统网 > 汉典 > 本地已有」的权威性排序，字统网结果优先覆盖，
    本地数据补齐其余部分。``？`` 是它标记的未知部件，作空处理。
    """
    out: dict[str, str] = {}
    path = os.path.join(RAW, "makemeahanzi", "dictionary.txt")
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            decomp = rec.get("decomposition")
            if decomp and decomp not in ("？", "?"):
                out[rec["character"]] = decomp
    return out


def ids_children(ids: str | None) -> list[str]:
    """一条 IDS 的直接子符号（去掉算子）。``#(H)`` 这类笔画编码返回空。"""
    if not ids or ids.startswith("#"):
        return []
    return [c for c in ids
            if c not in IDS_OPERATORS and c not in "()" and c not in "？?"]


def components_of(ch: str, ids_table: dict[str, str],
                  memo: dict[str, frozenset[str]], depth: int = 0) -> frozenset[str]:
    """递归收集一个字的全部部件（含嵌套）。用于 ``[日]`` 命中 ``白``。"""
    if ch in memo:
        return memo[ch]
    memo[ch] = frozenset()          # 占位防环
    if depth >= MAX_COMPONENT_DEPTH:
        return memo[ch]
    acc: set[str] = set()
    for child in ids_children(ids_table.get(ch)):
        acc.add(child)
        if child != ch:
            acc |= components_of(child, ids_table, memo, depth + 1)
    memo[ch] = frozenset(acc)
    return memo[ch]


def parse_cedict() -> tuple[list[tuple], Counter]:
    """返回 (词条列表, (字, 位置, 音节) 计数) 供 jieba 独有词推定读音。"""
    path = os.path.join(RAW, "cedict", "cedict_ts.u8")
    entries: list[tuple] = []
    pos_counts: Counter = Counter()
    # 先收集，稍后统一按词形合并多套读音
    tmp: dict[str, dict] = {}

    for line in open(path, encoding="utf-8"):
        if line.startswith("#"):
            continue
        parts = line.split(" ")
        if len(parts) < 3:
            continue
        trad, simp = parts[0], parts[1]
        m = re.search(r"\[([^\]]*)\]", line)
        if not m:
            continue
        if not (MIN_LEN <= len(simp) <= MAX_LEN) or not all(is_han(c) for c in simp):
            continue

        pinyin_str = m.group(1).replace(",", " ").replace("·", " ")
        toks: list[str] = []
        for raw_tok in pinyin_str.split():
            # 正常是一个音节一个 token；个别条目把两个粘在一起（shi2ke4）
            toks.extend(split_syllables(raw_tok)
                        if sum(c.isdigit() for c in raw_tok) > 1 else [raw_tok])
        if "xx" in pinyin_str:
            continue                       # 无定义读音（々 等），整条丢弃读音

        syls: list[Syllable] = []
        ok = True
        for tok in toks:
            try:
                syls.append(parse_syllable(tok))
            except ValueError:
                ok = False
                break
        if not ok or len(syls) != len(simp):
            continue

        cstart = line.rfind("/") if "/" in line else -1
        first = line.find("/", m.end())
        definition = ""
        if first >= 0 and cstart > first:
            definition = line[first:cstart + 1].strip("/").replace("/", "；")
        is_idiom = "idiom" in definition.lower()

        rec = tmp.setdefault(simp, {"trad": trad, "defs": set(), "idiom": False})
        rec["defs"].add(definition)
        rec["idiom"] = rec["idiom"] or is_idiom
        rec.setdefault("reads", [])
        t = tuple(syls)
        if t not in rec["reads"]:
            rec["reads"].append(t)

        n = len(simp)
        for i, s in enumerate(syls):
            pos_counts[(simp[i], "s", i, s)] += 1
            pos_counts[(simp[i], "e", n - 1 - i, s)] += 1

    for simp, rec in tmp.items():
        entries.append((simp, rec["trad"], tuple(rec["reads"]),
                        "；".join(sorted(d for d in rec["defs"] if d)),
                        int(rec["idiom"])))
    return entries, pos_counts


def load_jieba() -> dict[str, int]:
    freq: dict[str, int] = {}
    path = os.path.join(RAW, "jieba", "jieba-master", "jieba", "dict.txt")
    for line in open(path, encoding="utf-8"):
        parts = line.split()
        if len(parts) < 2:
            continue
        w = parts[0]
        if MIN_LEN <= len(w) <= MAX_LEN and all(is_han(c) for c in w):
            freq[w] = max(freq.get(w, 0), int(parts[1]))
    return freq


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------

def build(verbose: bool = True) -> dict:
    t0 = time.time()
    log = (lambda *a: print(*a, flush=True)) if verbose else (lambda *a: None)

    strokes_u, radical_no = load_unihan()
    pinyin_data = load_pinyin_data()
    zdic = load_zdic()
    zt_ids, difficult = load_zitools()
    mm_ids = load_makemeahanzi()
    # 权威性：字统网 > 本地 makemeahanzi
    ids_table = {**mm_ids, **zt_ids}
    cedict, pos_counts = parse_cedict()
    jieba_freq = load_jieba()
    log(f"载入: Unihan {len(strokes_u)} 字, pinyin-data {len(pinyin_data)} 字, "
        f"汉典 {len(zdic)} 字, IDS {len(ids_table)} 字"
        f"（字统网 {len(zt_ids)} + makemeahanzi {len(mm_ids)}，字统网优先）, "
        f"CEDICT {len(cedict)} 词, jieba {len(jieba_freq)} 词")

    # ---- 单字表 ----
    chars_needed: set[str] = set()
    for w, _, _, _, _ in cedict:
        chars_needed.update(w)
    for w in jieba_freq:
        chars_needed.update(w)
    log(f"语料共涉及 {len(chars_needed)} 个不同汉字")

    cmemo: dict[str, frozenset[str]] = {}
    chars: list[tuple] = []
    struct_missing = 0
    for ch in sorted(chars_needed):
        z = zdic.get(ch, {})
        rno = radical_no.get(ch)
        kangxi_ch = KANGXI[rno - 1] if rno and 1 <= rno <= 214 else None
        radical = z.get("radical_zdic") or kangxi_ch
        structure = z.get("structure")
        if structure is None:
            struct_missing += 1

        comp = set(components_of(ch, ids_table, cmemo))
        if kangxi_ch:
            comp.add(kangxi_ch)
        if radical:
            comp.add(radical)

        readings = pinyin_data.get(ch, ())
        chars.append((
            ch,
            strokes_u.get(ch) if strokes_u.get(ch) is not None else z.get("strokes_zdic"),
            radical, rno, structure,
            "zdic" if z.get("structure") else None,
            z.get("liushu"),
            ids_table.get(ch),
            json.dumps([[s.initial, s.final, s.tone] for s in readings],
                       ensure_ascii=False),
            len(readings),
            json.dumps(sorted(comp), ensure_ascii=False),
        ))
    log(f"结构数据缺失 {struct_missing} 字（不参与结构筛选，结果页会提示）")

    # ---- 字频（用于零频词兜底排序）----
    char_freq: Counter = Counter()
    for w, f in jieba_freq.items():
        for c in w:
            char_freq[c] += f

    # ---- 词表 ----
    cedict_map = {w: (trad, reads, definition, idiom)
                  for w, trad, reads, definition, idiom in cedict}
    all_words = set(cedict_map) | set(jieba_freq)

    def infer_reading(word: str) -> tuple[Syllable, ...]:
        """jieba 独有词：逐位置取联合概率最高的读音。

        比单用某个「首选读音」准得多——「长」在词首多读 zhǎng、在词尾多读 cháng。
        """
        n = len(word)
        out: list[Syllable] = []
        for i, c in enumerate(word):
            cands = pinyin_data.get(c, ())
            if not cands:
                return ()
            best, best_score = cands[0], -1
            for s in cands:
                score = (pos_counts.get((c, "s", i, s), 0)
                         + pos_counts.get((c, "e", n - 1 - i, s), 0))
                if score > best_score:
                    best, best_score = s, score
            out.append(best)
        return tuple(out)

    rows: list[tuple] = []
    reading_rows: list[tuple] = []
    wid = 0
    stats = Counter()
    for w in sorted(all_words):
        freq = jieba_freq.get(w, 0)
        in_cedict = w in cedict_map
        if in_cedict:
            trad, reads, definition, idiom = cedict_map[w]
            sources = ["cedict"] * len(reads)
        else:
            trad, definition, idiom = None, "", False
            inf = infer_reading(w)
            if inf:
                reads, sources = (inf,), ("inferred",)
                stats["inferred"] += 1
            else:
                reads, sources = (), ()
                stats["no_reading"] += 1

        tier = 0 if freq > 0 else (1 if in_cedict else 2)
        aux = float(sum(char_freq.get(c, 0) for c in w))
        displays = tuple(" ".join(format_pinyin(s.initial, s.final, s.tone)
                                  for s in r) for r in reads)

        rows.append((wid, w, trad, len(w), freq, tier, aux,
                     int(bool(reads)), displays[0] if displays else None,
                     int(idiom), definition))
        for ri, r in enumerate(reads):
            reading_rows.append((wid, ri,
                                 " ".join(f"{s.initial}/{s.final}/{s.tone}" for s in r),
                                 sources[ri]))
        wid += 1

    log(f"词表 {len(rows)} 条：CEDICT {len(cedict_map)}，jieba 独有 "
        f"{len(all_words) - len(cedict_map)}（其中推定读音 {stats['inferred']}，"
        f"无读音可用 {stats['no_reading']}）")

    # ---- 写库 ----
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    PRAGMA journal_mode=OFF;
    PRAGMA synchronous=OFF;
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE chars (
      ch TEXT PRIMARY KEY, strokes INTEGER, radical TEXT, radical_no INTEGER,
      structure TEXT, structure_src TEXT, liushu TEXT, ids TEXT,
      readings TEXT NOT NULL, n_readings INTEGER NOT NULL,
      components TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX idx_chars_strokes   ON chars(strokes);
    CREATE INDEX idx_chars_structure ON chars(structure);
    CREATE TABLE words (
      id INTEGER PRIMARY KEY, word TEXT NOT NULL UNIQUE, word_trad TEXT,
      length INTEGER NOT NULL, freq INTEGER NOT NULL DEFAULT 0,
      tier INTEGER NOT NULL, sort_aux REAL NOT NULL DEFAULT 0,
      has_pinyin INTEGER NOT NULL DEFAULT 0, pinyin_display TEXT,
      is_idiom INTEGER NOT NULL DEFAULT 0, definition TEXT
    );
    CREATE INDEX idx_words_len  ON words(length, tier, freq DESC, sort_aux DESC);
    CREATE INDEX idx_words_freq ON words(freq DESC);
    CREATE TABLE word_readings (
      word_id INTEGER NOT NULL, ridx INTEGER NOT NULL, syllables TEXT NOT NULL,
      source TEXT NOT NULL, PRIMARY KEY (word_id, ridx)
    ) WITHOUT ROWID;
    """)
    con.executemany("INSERT INTO chars VALUES (?,?,?,?,?,?,?,?,?,?,?)", chars)
    con.executemany("INSERT INTO words VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO word_readings VALUES (?,?,?,?)", reading_rows)

    meta = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_chars": str(len(chars)), "n_words": str(len(rows)),
        "n_readings": str(len(reading_rows)),
        "struct_missing": str(struct_missing),
        "cedict_sha1": sha1(os.path.join(RAW, "cedict", "cedict_ts.u8")),
        "jieba_sha1": sha1(os.path.join(RAW, "jieba", "jieba-master", "jieba",
                                        "dict.txt")),
        "unihan_sha1": sha1(os.path.join(RAW, "unihan", "Unihan_IRGSources.txt")),
    }
    con.executemany("INSERT INTO meta VALUES (?,?)", sorted(meta.items()))
    con.commit()
    con.close()
    log(f"已写 {DB_PATH}（{os.path.getsize(DB_PATH)/1e6:.1f} MB，"
        f"{time.time()-t0:.1f} 秒）")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 data/cn_nutri.db")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    build(verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
