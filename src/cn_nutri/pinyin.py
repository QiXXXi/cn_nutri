"""拼音规范化：把各种写法的拼音音节拆成 (声母, 韵母, 声调)。

内部约定
--------
韵母一律用**书写形**（缩写）存储：``iu`` / ``ui`` / ``un``，
不用 ``iou`` / ``uei`` / ``uen``；``ü`` 一律写作 ASCII 的 ``v``，
以便在 SQLite / JSON / 查询语法里安全传递。展示层再转回 ``ü``。

这样查询侧也变得宽容：``canonical_final("iou")`` 与 ``canonical_final("iu")``
归一到同一个 ``"iu"``，用户写哪种都命中。
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = [
    "Syllable", "parse_syllable", "canonical_final", "canonical_initial",
    "display_final", "format_pinyin", "ALL_FINALS", "ALL_INITIALS",
    "ZERO_INITIAL", "is_final", "is_initial",
]


class Syllable(NamedTuple):
    """一个音节。``initial`` 为空串表示零声母；``tone`` 为 1–5，5 是轻声。"""

    initial: str
    final: str
    tone: int

    def __str__(self) -> str:
        return f"{self.initial or '∅'}+{display_final(self.final)}+{self.tone}"


#: 零声母在查询语法中的写法
ZERO_INITIAL = "xx"

# 声母表。按长度降序排列，保证 zh/ch/sh 先于 z/c/s 匹配。
ALL_INITIALS = (
    "zh", "ch", "sh",
    "b", "p", "m", "f", "d", "t", "n", "l",
    "g", "k", "h", "j", "q", "x", "r", "z", "c", "s",
)

# 韵母表（内部 ASCII 形）。与 ALL_INITIALS 交集为空——
# 这是查询语法「不同类条件不歧义」的基础，tests 里有断言守着。
ALL_FINALS = frozenset({
    "a", "o", "e", "i", "u", "v", "er", "ng",       # ng 用于「嗯」等成音节鼻音
    "ai", "ei", "ao", "ou", "an", "en", "ang", "eng", "ong",
    "ia", "ie", "iao", "iu", "ian", "in", "iang", "ing", "iong",
    "ua", "uo", "uai", "ui", "uan", "un", "uang", "ueng",
    "ve", "van", "vn",
})

# 带调符号 -> (基元音, 调号)
_TONE_MARKS: dict[str, tuple[str, int]] = {}
for _base, _marks in (
    ("a", "āáǎà"), ("e", "ēéěè"), ("i", "īíǐì"),
    ("o", "ōóǒò"), ("u", "ūúǔù"), ("v", "ǖǘǚǜ"),
):
    for _i, _ch in enumerate(_marks, start=1):
        _TONE_MARKS[_ch] = (_base, _i)
_TONE_MARKS.update({"ń": ("n", 2), "ň": ("n", 3), "ǹ": ("n", 4), "ḿ": ("m", 2)})

# 零声母书写形 -> 真实韵母。y 系与 w 系分开，避免 "u" 的歧义
# （yu 的韵母是 ü，wu 的韵母是 u）。
_Y_MAP = {
    "i": "i", "in": "in", "ing": "ing",
    "a": "ia", "ao": "iao", "e": "ie", "ou": "iu",
    "an": "ian", "ang": "iang", "ong": "iong", "o": "o",
    "u": "v", "ue": "ve", "uan": "van", "un": "vn",
}
_W_MAP = {
    "u": "u", "a": "ua", "o": "uo", "ai": "uai", "ei": "ui",
    "an": "uan", "en": "un", "ang": "uang", "eng": "ueng",
}

# 解析用前缀表：除真声母外，还含零声母书写形 y/w，
# 以及成音节鼻音 ng（须排在 n 之前，故与 ALL_INITIALS 一起按长度降序排）。
# 注意 y/w 不属于 ALL_INITIALS——它们不是声母，只是零声母的拼写形式。
# 同长度时按字典序定序，而不是沿用 set 的迭代序——后者受 PYTHONHASHSEED 影响，
# 会让导出的 tables.json 每次都不一样。行为不受影响：同一长度上不可能有两个
# 不同的前缀同时是某个音节的开头。
_PARSE_PREFIXES = tuple(
    sorted(set(ALL_INITIALS) | {"y", "w", "ng"}, key=lambda s: (-len(s), s))
)

# 成音节鼻音（嗯 ń/ňg、呣 ḿ、唔 ń …）。这类音节不属于任何声母+韵母的组合，
# 一律记为韵母 "ng"——m/ng 的音位差别在词库中仅涉个位数汉字，不值得为它
# 在韵母空间里再开一个与声母 "m" 冲突的名字。
_SYLLABIC_NASALS = frozenset({"n", "ng", "m", "hm", "hng"})

# 零声母音节的**书写形**：内部 (∅, final) -> 实际怎么拼。
# 这是 _Y_MAP / _W_MAP 的逆映射。少了它，「样」会显示成 iàng 而不是 yàng。
# 未列出的（a o e ai ei ao ou an en ang eng er ng）本身就是音节，原样输出。
_ZERO_FORM = {
    "i": "yi", "in": "yin", "ing": "ying",
    "ia": "ya", "iao": "yao", "ie": "ye", "iu": "you",
    "ian": "yan", "iang": "yang", "iong": "yong",
    "v": "yu", "ve": "yue", "van": "yuan", "vn": "yun",
    "u": "wu", "ua": "wa", "uo": "wo", "uai": "wai", "ui": "wei",
    "uan": "wan", "un": "wen", "uang": "wang", "ueng": "weng",
}

# 全写 -> 书写形
_FINAL_ALIASES = {"iou": "iu", "uei": "ui", "uen": "un"}

# 内部形 -> 展示形
_DISPLAY_FINAL = {"v": "ü", "ve": "üe", "van": "üan", "vn": "ün"}

# 展示时调号标在哪个字母上（a/o/e 优先，其次末位元音）
_TONE_MARK_TABLE = {
    "a": "āáǎà", "o": "ōóǒò", "e": "ēéěè",
    "i": "īíǐì", "u": "ūúǔù", "v": "ǖǘǚǜ",
}
# display_final 把内部的 v 渲染成 ü，所以调号表也必须认这个字形——
# 少了它，`hü` 会静默丢掉声调。
_TONE_MARK_TABLE["ü"] = _TONE_MARK_TABLE["v"]


def _normalize_umlaut(s: str) -> str:
    """``u:`` / ``ü`` / ``v`` 统一成 ASCII 的 ``v``。"""
    return s.replace("u:", "v").replace("ü", "v")


def _strip_tone(s: str) -> tuple[str, int]:
    """剥掉调号，返回 (无调拼音, 调号)。无调号时调号为 5（轻声）。"""
    tone = 5
    out = []
    for ch in s:
        hit = _TONE_MARKS.get(ch)
        if hit is None:
            out.append(ch)
        else:
            base, tone = hit
            out.append(base)
    return "".join(out), tone


def is_final(text: str) -> bool:
    """该写法是否是一个合法韵母（宽容接受全写形与 ü 写法）。"""
    return canonical_final(text) is not None


def is_initial(text: str) -> bool:
    """该写法是否是一个合法声母（``xx`` 表示零声母）。"""
    return canonical_initial(text) is not None


def canonical_final(final: str) -> str | None:
    """把任意写法的韵母归一到内部形；不合法返回 None。"""
    f = _normalize_umlaut(final.strip().lower())
    f = _FINAL_ALIASES.get(f, f)
    return f if f in ALL_FINALS else None


def canonical_initial(initial: str) -> str | None:
    """把任意写法的声母归一到内部形；``xx``/空/``∅`` 归一到零声母（空串）。"""
    i = initial.strip().lower()
    if i in ("", ZERO_INITIAL, "∅", "0"):
        return ""
    return i if i in ALL_INITIALS else None


def parse_syllable(syllable: str) -> Syllable:
    """解析一个拼音音节。同时支持数字调与调号两种写法。

    >>> parse_syllable("zhang1")
    Syllable(initial='zh', final='ang', tone=1)
    >>> parse_syllable("zhāng")
    Syllable(initial='zh', final='ang', tone=1)
    >>> parse_syllable("lv4")
    Syllable(initial='l', final='v', tone=4)
    >>> parse_syllable("yu2")
    Syllable(initial='', final='v', tone=2)
    >>> parse_syllable("wen2")
    Syllable(initial='', final='un', tone=2)
    >>> parse_syllable("er2")
    Syllable(initial='', final='er', tone=2)
    >>> parse_syllable("dui4")
    Syllable(initial='d', final='ui', tone=4)
    """
    s = syllable.strip().lower()
    if not s:
        raise ValueError("空音节")

    tone = 5
    if s[-1].isdigit():
        tone = int(s[-1])
        s = s[:-1]
        if s and s[-1].isdigit():  # 形如 "12" 的脏数据
            raise ValueError(f"非法音节: {syllable!r}")
    else:
        s, tone = _strip_tone(s)

    s = _normalize_umlaut(s)
    if not s:
        raise ValueError(f"非法音节: {syllable!r}")

    if s in _SYLLABIC_NASALS:
        return Syllable("", "ng", tone)

    # 儿化的成音节后缀：CC-CEDICT 把「一下儿」的「儿」记作 r5，而非 er2。
    # 归一到 er，使 [er] 能同时命中独立读音与儿化后缀。
    if s == "r":
        return Syllable("", "er", tone)

    initial = ""
    rest = s
    for cand in _PARSE_PREFIXES:
        if s.startswith(cand):
            initial, rest = cand, s[len(cand):]
            break

    if initial == "y":
        rest = _Y_MAP.get(rest, rest)
        initial = ""
    elif initial == "w":
        rest = _W_MAP.get(rest, rest)
        initial = ""
    elif initial in ("j", "q", "x") and rest.startswith("u"):
        # j/q/x 后的 u 实际是 ü
        rest = "v" + rest[1:]

    final = _FINAL_ALIASES.get(rest, rest)
    if final not in ALL_FINALS:
        raise ValueError(f"非法音节: {syllable!r}（韵母 {final!r} 不合法）")
    return Syllable(initial, final, tone)


def display_final(final: str) -> str:
    """内部形 -> 展示形（``v`` 显示为 ``ü``）。"""
    return _DISPLAY_FINAL.get(final, final)


def format_pinyin(initial: str, final: str, tone: int) -> str:
    """重组出带调号的拼音，如 ``zhǎng``。轻声不加号。

    >>> format_pinyin("zh", "ang", 3), format_pinyin("", "iang", 4)
    ('zhǎng', 'yàng')
    >>> format_pinyin("", "v", 2), format_pinyin("", "un", 2)
    ('yú', 'wén')
    """
    if initial == "":
        word = _ZERO_FORM.get(final, display_final(final))
    elif initial in ("j", "q", "x"):
        # j/q/x 后面只可能是 ü 音，正字法上不写两点：jü -> ju
        word = initial + display_final(final).replace("ü", "u")
    else:
        word = initial + display_final(final)
    if tone == 5 or not word:
        return word

    # 调号优先级：a > o > e > 末位元音
    idx = -1
    for target in ("a", "o", "e"):
        idx = word.find(target)
        if idx >= 0:
            break
    if idx < 0:
        for i in range(len(word) - 1, -1, -1):
            if word[i] in _TONE_MARK_TABLE:
                idx = i
                break
    if idx < 0:
        return word

    base = word[idx]
    mark = _TONE_MARK_TABLE.get(base)
    if mark is None:
        return word
    return word[:idx] + mark[tone - 1] + word[idx + 1:]


#: 客户端引擎（web/engine.js）需要的全部拼音词汇表，与 query.CLIENT_TABLES 配套，
#: 由 tools/build_client_data.py 导成 web/data/tables.json。JS 侧只移植**逻辑**
#: （canonical_* / parse_syllable / format_pinyin），字表一律来自这里。
CLIENT_TABLES = {
    "initials": list(ALL_INITIALS),
    "finals": sorted(ALL_FINALS),
    "zero_initial": ZERO_INITIAL,
    "final_aliases": dict(_FINAL_ALIASES),
    "display_final": dict(_DISPLAY_FINAL),
    # display_final 会把内部的 v 渲染成 ü，所以调号表也必须认这个字形
    "tone_mark_table": {k: v for k, v in _TONE_MARK_TABLE.items()},
    "tone_marks": {ch: list(v) for ch, v in _TONE_MARKS.items()},
    "y_map": dict(_Y_MAP),
    "w_map": dict(_W_MAP),
    "zero_form": dict(_ZERO_FORM),
    "syllabic_nasals": sorted(_SYLLABIC_NASALS),
    "parse_prefixes": list(_PARSE_PREFIXES),
}
