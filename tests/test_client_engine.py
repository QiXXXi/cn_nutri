"""客户端引擎（src/cn_nutri/web/engine.js）与 Python 参考实现的对拍。

移植了 800 多行 JS，靠肉眼是保证不了「没写错」的。这里把同一批查询分别喂给
两边，逐字段比对 **整份响应**——不只是结果条数和词面，还包括 spans、命中读音
下标、读音来源、拼音串、释义、以及 excluded_missing / missing_chars / scanned
这些统计量。少比一个字段，那个字段就会静静地漂。

要用 node。没装 node 就整模块 skip——**不是通过**，是没验。所以别把这里的
skip 当成绿灯。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "cn_nutri.db")
CLI = os.path.join(ROOT, "tools", "engine_cli.js")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_client_data as bcd  # noqa: E402
from cn_nutri.corpus import load_corpus  # noqa: E402
from cn_nutri.matcher import Matcher  # noqa: E402
from cn_nutri.query import parse_pattern  # noqa: E402
from cn_nutri.search import search  # noqa: E402
from cn_nutri.server import parse_payload, search_payload  # noqa: E402

if not os.path.exists(DB):
    pytest.skip("没有 data/cn_nutri.db：先跑 python -I tools/build_db.py",
                allow_module_level=True)


def _find_node() -> str | None:
    """PATH 上找不到就探默认安装位置。

    刚装完 Node 时当前 shell 的 PATH 还是旧的（装之前就起了），所以要兜一下。
    """
    for name in ("node", "node.exe"):
        p = shutil.which(name)
        if p:
            return p
    for guess in (r"C:\Program Files\nodejs\node.exe",
                  r"C:\Program Files (x86)\nodejs\node.exe"):
        if os.path.exists(guess):
            return guess
    return None


NODE = _find_node()
if NODE is None:
    pytest.skip("没有 node：客户端引擎对不了拍（winget install OpenJS.NodeJS.LTS）",
                allow_module_level=True)


# ---------------------------------------------------------------------------
# 查询清单
# ---------------------------------------------------------------------------

#: 用户给的完整示例：开头是 `[`、结尾是 `】`，全半角混用，夹杂空格与无法识别的词条
MESSY = ("[氵，m ,a, e，~3,b,左右，8-,(沐露二勾恶感),10，i,  xx ， 上下，xx, ~4，u，v,16+,o，"
         "这是啥, idontknow】")

#: (查询, 选项)。挑的是能压到不同代码路径的，不是随手凑数：
#: 定长 / 含 * 的回溯 / 数据缺失 / 空约束 / 各排序 / 分页 / 语法错。
SEARCH_JOBS: list[tuple[str, dict]] = [
    # README 的头号例子，也是本项目存在的理由
    ("[a,an,ang][~3,~4][7-][独体,左右]", {"limit": 30}),
    ("[a,an,ang][~3,~4][7-][独体,左右]", {"limit": 40, "offset": 200}),
    ("[a,an,ang][~3,~4][7-][独体,左右]", {"limit": 25, "sort": "strokes"}),
    ("[a,an,ang][~3,~4][7-][独体,左右]", {"limit": 25, "sort": "pinyin"}),
    ("[a,an,ang][~3,~4][7-][独体,左右]", {"limit": 25, "sort": "len"}),

    # 含 * ：走回溯，且要扫全量 37.6 万词
    ("*[氵]*", {"limit": 30}),
    ("*[氵]*", {"limit": 30, "sort": "len"}),
    ("*[左右][7-]*", {"limit": 40}),

    # 数据缺失路径：excluded_missing / missing_chars 必须两边一样
    ("[7-][7-][7-][7-]", {"limit": 10}),
    ("*[16+]*", {"limit": 20}),

    # 通配与字面
    ("?[~1]", {"limit": 15}),
    ("张[a,an]", {"limit": 40}),
    ("[er][~2]", {"limit": 20}),
    ("[xx][a]", {"limit": 25}),

    # 部首 / 拆字 / 韵母归一化：必须走 * 的形式。
    ("*[日]*", {"limit": 30}),
    ("*[⿰弓长]*", {"limit": 10}),
    ("*[⿱艹化]*", {"limit": 10}),
    ("*[iou]*", {"limit": 15}),
    ("*[ü]*", {"limit": 15}),
    ("*[氵,~3]*", {"limit": 10}),          # 部首 ∩ 声调：异类取交集
    ("[氵][~3]", {"limit": 10}),           # 定长版，走 scanFixed 而不是 globMatch

    # ★ 单字词：语料的 7,533 个字现在都是 1 字词，所以**单个 [] 不再恒为空**。
    # 这几条正是「加单字」这次改动的锚点，命中数逐条钉在 GOLDEN_TOTALS 里。
    # 注意 `[ang]`：行/上/让…只要**任一套**读音的韵母是 ang 就算命中——单字没有
    # 词级语境，读音的歧义是并列收下而不是挑一个。
    ("[]", {"limit": 20}),                 # 空槽 = 任意一字 -> 全部单字
    ("[日]", {"limit": 20}),
    ("[⿰弓长]", {"limit": 10}),
    ("[~1]", {"limit": 20}),
    ("[ang]", {"limit": 20}),

    # 空约束 / 无法识别的词条 -> 整组被忽略，退化成「任意一个字」
    ("[镶嵌]", {"limit": 10}),
    ("[这是啥,idontknow]", {"limit": 5}),

    # `[]` 与 `?` 在引擎里必须**完全等价**（表单里它们倒是两种卡片：`?` 原文照写），
    # 与上面那条 `?[~1]` 同数才算过。
    ("[][~1]", {"limit": 20}),

    # 成语筛选
    ("[左右][左右]", {"idiomOnly": True, "limit": 20}),
    ("[a][a][a][a]", {"limit": 30}),

    # 用户给的完整示例：7 类约束、恰好忽略 2 项
    (MESSY, {"limit": 10}),

    # 相等/不等标记。这是唯一跨字位的一类条件，也是新增字段最容易漂的地方：
    # 命中数、spans、to_dict 的 eq/neq 键序，两边必须逐字段一致。
    ("[=A][=A]", {"limit": 20}),                       # 叠字
    ("[=A][][=A]", {"limit": 20}),                      # 第 1、3 字相同
    ("[=A][=B][=A][=B]", {"limit": 20}),                # ABAB，两组各自相等
    ("[=A][][=A][≠A]", {"limit": 20}),                  # 用户原例：1=3，第 4 字不同
    ("[=A][≠A][=A]", {"limit": 20}),                    # 不等夹在中间
    ("[=A][=B][≠A]", {"limit": 20}),                    # 一个字位带多个标记
    ("[≠A][=A]", {"limit": 20, "sort": "len"}),         # 不等写在等之前 -> 悬空义务
    ("[≠A][=A]", {"limit": 20, "sort": "pinyin"}),
    ("[≠A][~1][=A]", {"limit": 20}),                    # 悬空 + 别的条件同槽
    ("[=a][=A]", {"limit": 10}),                        # 大小写归一
    ("[＝Ａ][＝Ａ]", {"limit": 10}),                     # 全角归一
    ("*[=A][=A]*", {"limit": 20}),                      # 标记 × 回溯：memo 键要含绑定态
    ("[=A][][=A][≠A][=A]", {"limit": 10}),              # 全库只有 1 个词

    # 子集（$名）。第二条跨字位条件，也是**唯一成员能有两个字**的一类：判定要看
    # 后一个字，所以跟标记一样走 _scan/_glob，不走 match_prepared。定长与回溯
    # 两条路各来一条，再加并集、纯两字成员、未知名字、裸写五种形态。
    ("[$姓氏][][][]", {"limit": 20}),                   # 司马相如 / 令狐大哥 靠两字成员命中
    ("[$姓氏][][][]", {"limit": 20, "sort": "len"}),
    ("[$姓氏]", {"limit": 20}),                         # 只有单字成员起作用
    ("[$节气][]", {"limit": 30}),                       # 成员全是两字：立春、霜降…
    ("*[$姓氏]*", {"limit": 20}),                       # 回溯路
    ("[$朝代,$颜色][~2]", {"limit": 20}),               # 组内多个子集名取并集
    ("[$节气]", {"limit": 10}),                         # 见 EXPECTED_EMPTY
    ("[$姓氐]", {}),                                    # 未知子集名 -> 语法错
    ("$姓氏", {}),                                      # 裸写在 [] 外面 -> 语法错

    # 语法错：必须两边都报同一个 reason，且一条结果都不出
    ("[unclosed", {}),
    ("abc", {}),
    ("张\\", {}),
    ("(未闭", {}),
    ("[≠A][~1]", {}),       # ≠A 全式找不到 =A
    ("[=A,≠A]", {}),        # 同一字位自相矛盾
]

#: 本来就该**没有结果**的作业，逐条有理由。剩下的作业一条都不许为空——
#: 两边都返回空时比对也是绿的，那样套子等于没测。
#
#: 加单字词之后只剩 `[镶嵌]` 一条「合法但空」的用例了：`[日]`、`[⿰弓长]`、
#: MESSY、`[这是啥,idontknow]` 都变成有结果（见 GOLDEN_TOTALS）。
EXPECTED_EMPTY = {
    "[镶嵌]": "「镶嵌」在查询侧的 STRUCTURES 里**是有的**，所以它不是被忽略、"
              "也没退化成任意一字——是语料里那个原值写作「嵌套结构」，两边对不上，"
              "于是恒定不匹配。（原先这条注释说它「不在词表里、被忽略」，是错的。"
              "用「任意一字」试它：加单字词后 `[]` 命中 7533 条，而它仍是 0。）",
    "abc": "字面 'a' 后面不是汉字，逐字匹配到没有的词",
    "[unclosed": "方括号未闭合",
    "张\\": "转义符后无字符",
    "(未闭": "圆括号未闭合",
    "[≠A][~1]": "≠A 在这个表达式里找不到任何 =A",
    "[=A,≠A]": "同一个字位同时要相等又要不等，自相矛盾",
    "[$节气]": "成员全是两字（立春、霜降…），而它只给了一个字位。单字词里没有"
                "两字子串，所以恒为 0——这是「成员可以是两个字」那条规则的自然结果，"
                "不是条件被忽略了（拿「任意一字」试它：[] 命中 7533 条，它是 0）。",
    "[$姓氐]": "子集名不认识 -> 语法错，整条查询被拦住",
    "$姓氏": "子集条件裸写在方括号外面 -> 语法错",
}

#: 逐条钉死的命中数。查询在 SEARCH_JOBS 里出现过就行（选项不影响 total）。
#: Python 侧由上面的逐字段比对保证同值，这里在**客户端引擎**这一侧再卡一道：
#: 两边一起漂（比如都漏读了一批词）时，只有绝对值能发现。
GOLDEN_TOTALS: dict[str, int] = {
    "[]": 7533,                     # = 语料字数。空槽不约束任何东西
    "[这是啥,idontknow]": 7533,     # 整组无法识别 -> 同样退化成任意一字
    "[日]": 680,                    # 部首或拆字里有「日」的单字
    "[⿰弓长]": 1,                   # 只有「张」
    "[~1]": 2491,
    "[ang]": 283,
    "?[~1]": 26712,
    "[][~1]": 26712,                # 与上一行必须相等
    MESSY: 1,                       # 七类条件叠在一起，恰好剩「沐」
    # 子集。姓氏两条的差就是「两字成员真的在起作用」：少了复姓那一批，4 字词会掉很多。
    "[$姓氏]": 663,                 # 单字词里是姓氏的
    "[$姓氏][][][]": 22016,
    "[$节气][]": 24,                # 恰好就是那 24 个节气词
}

PARSE_JOBS: list[str] = [
    "[a,an][~1]",
    "[氵，m ,a]",
    "张?*[a]",
    "[7-][独体](沐露)⿰弓长",
    "[a,an,ang][~3,~4][7-][独体,左右]",
    "[镶嵌]",
    "[这是啥,idontknow]",
    "**??",
    "",
    "[",
    "(未闭",
    "\\",
    "张\\",
    "[]()a[e]",
    # 标记：合法形态、退化形态、两条报错、别名与全角
    "[=A][=A]",
    "[=A][][=A][≠A]",
    "[≠A][=A]",
    "[=A,≠A]",
    "[≠A][~1]",
    "[=A,~3,≠B][=B]",
    "[!=A][=A]",
    "[＝Ａ][！＝Ａ]",
    # 子集：合法、带别的条件、未知名字、空名字、裸写
    "[$姓氏]",
    "[$姓氏][][][]",
    "[$姓氏,~3]",
    "[$朝代,$颜色]",
    "[$姓氐]",
    "[$]",
    "$姓氏",
]


# ---------------------------------------------------------------------------
# 跑一遍 JS
# ---------------------------------------------------------------------------

def _run_batch(data_dir: str, jobs: list[dict]) -> list:
    payload = json.dumps(jobs, ensure_ascii=False).encode("utf-8")
    proc = subprocess.run([NODE, CLI, data_dir, "--batch"],
                          input=payload, capture_output=True, timeout=900)
    if proc.returncode != 0:
        raise AssertionError(
            "engine_cli.js 退出码 {}：\n{}".format(
                proc.returncode, proc.stderr.decode("utf-8", "replace")[:4000]))
    out = proc.stdout.decode("utf-8")
    try:
        return json.loads(out)
    except ValueError:
        raise AssertionError("engine_cli.js 的输出不是 JSON：\n" + out[:4000])


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory) -> str:
    """现建一份词库再对拍。

    不复用 web/data/ 下已有的产物：那是上一次构建留下的，拿它去验这一次的引擎，
    测的就不是同一份数据了。
    """
    d = tmp_path_factory.mktemp("engine_data")
    b = bcd.build(DB)
    bcd.write_bundle(b, str(d / "corpus.bin"))
    (d / "tables.json").write_text(
        json.dumps(bcd.tables_json(b, bcd.meta_of(DB)), ensure_ascii=False),
        encoding="utf-8")
    return str(d)


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(DB)


@pytest.fixture(scope="module")
def js_all(data_dir):
    """一次进程跑完全部作业——每查询起一个 node 要 2 秒解码，那套子跑不完。"""
    jobs = ([{"q": q, "opts": o} for q, o in SEARCH_JOBS]
            + [{"q": q, "parse": True} for q in PARSE_JOBS]
            + [{"stats": True}])
    out = _run_batch(data_dir, jobs)
    assert len(out) == len(jobs), "返回条数与作业数对不上"
    n = len(SEARCH_JOBS)
    return {
        "search": out[:n],
        "parse": out[n:n + len(PARSE_JOBS)],
        "stats": out[-1],
    }


# ---------------------------------------------------------------------------
# 逐字段比对
# ---------------------------------------------------------------------------

def diff(a, b, path: str = "") -> str | None:
    """找出第一处不同，返回可读路径。比 assert a == b 好定位得多。"""
    if isinstance(a, bool) != isinstance(b, bool):
        return f"{path}: {a!r} != {b!r}（一边是布尔）"
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
            return f"{path}: 键不同，JS 多 {only_a}，Python 多 {only_b}"
        for k in a:
            d = diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: 长度 JS={len(a)} Python={len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if a != b:
        return f"{path}: JS={a!r} Python={b!r}"
    return None


def _strip_timing(obj):
    """耗时两边天然不同，不参与比对。"""
    if isinstance(obj, dict):
        return {k: _strip_timing(v) for k, v in obj.items() if k != "elapsed_ms"}
    if isinstance(obj, list):
        return [_strip_timing(v) for v in obj]
    return obj


def compare(js, py, label: str) -> None:
    d = diff(_strip_timing(js), _strip_timing(py), label)
    assert d is None, d


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("i", range(len(SEARCH_JOBS)),
                         ids=[f"{q} {o}" for q, o in SEARCH_JOBS])
def test_search_matches_python(js_all, corpus, i):
    q, opts = SEARCH_JOBS[i]
    res = search(corpus, q,
                 limit=opts.get("limit", 50),
                 offset=opts.get("offset", 0),
                 sort=opts.get("sort", "freq"),
                 idiom_only=opts.get("idiomOnly", False))
    compare(js_all["search"][i], search_payload(res, corpus), f"search[{i}] {q!r}")


@pytest.mark.parametrize("i", range(len(PARSE_JOBS)), ids=PARSE_JOBS)
def test_parse_matches_python(js_all, i):
    q = PARSE_JOBS[i]
    compare(js_all["parse"][i], parse_payload(parse_pattern(q)), f"parse[{i}] {q!r}")


def test_engine_header_agrees_with_python(js_all, corpus):
    """解码器的表头/桶统计。这条跟查询无关，专验它没把偏移数错。

    词表本身是逐条比对的（看下面那条），这里只比总量与分桶。
    """
    s = js_all["stats"]
    assert s["version"] == 2      # v2：词 flags 的 bit1 = 读音取自单字表
    assert s["n_words"] == len(corpus.all_words)
    assert s["n_chars"] == len(corpus.chars)
    assert s["max_len"] == max(len(w.word) for w in corpus.all_words)

    by_len: dict[int, int] = {}
    for w in corpus.all_words:
        by_len[len(w.word)] = by_len.get(len(w.word), 0) + 1
    assert {int(k): v for k, v in s["by_len"].items()} == by_len
    assert s["bucket_total"] == len(corpus.all_words)


def test_only_the_expected_jobs_come_back_empty(js_all):
    """空结果必须逐条有理由，别的作业一条都不许空。

    上面那些逐字段断言在「两边都返回空」时同样是绿的——查询写错了、或者引擎挂掉
    一半，会一起空着，套子看着全过其实没测到东西。这条把「哪些该空、哪些必须有
    结果」钉死：多一个空的就报出来。
    """
    unexpectedly_empty = [
        (i, q) for i, (q, _o) in enumerate(SEARCH_JOBS)
        if not js_all["search"][i]["results"] and q not in EXPECTED_EMPTY
    ]
    assert not unexpectedly_empty, (
        "这些查询本该有结果却是空的：" +
        "; ".join(f"[{i}] {q!r}" for i, q in unexpectedly_empty))

    wrongly_busy = [q for q in EXPECTED_EMPTY if q not in {q for q, _ in SEARCH_JOBS}]
    assert not wrongly_busy, f"EXPECTED_EMPTY 里这些查询已经不在用例里了：{wrongly_busy}"


def test_the_expected_empty_ones_are_actually_empty(js_all):
    """反向：上面列进 EXPECTED_EMPTY 的，必须真的空。

    不然有人把 SEARCH_JOBS 改坏了，这条清单就成了挡箭牌——反正「预期为空」。
    """
    busy = [q for i, (q, _o) in enumerate(SEARCH_JOBS)
            if q in EXPECTED_EMPTY and js_all["search"][i]["results"]]
    assert not busy, f"标着「预期为空」却出了结果：{busy}"


#: 计划 §九.3 写的是 240。**那个数没能复现**：数据齐备后实测 1590（含 CC-CEDICT
#: 收录的 367 条）。原值多半来自当时还没抓完的结构数据——结构判不了的字会被排除，
#: 所以数出来偏小。计数类断言以 tests/test_engine.py 的 GOLDEN_TOTAL 为准，这里
#: 重复一份是为了在**客户端引擎**这一侧也卡住同一个数，两边不该给出不同答案。
GOLDEN_TOTAL = 1590


def test_golden_totals(js_all):
    """命中数逐条钉死（见 GOLDEN_TOTALS）。"""
    got = {q: js_all["search"][i]["total"]
           for i, (q, _o) in enumerate(SEARCH_JOBS) if q in GOLDEN_TOTALS}
    missing = sorted(set(GOLDEN_TOTALS) - set(got))
    assert not missing, f"这些查询已经不在 SEARCH_JOBS 里了：{missing}"
    bad = {q: (got[q], GOLDEN_TOTALS[q]) for q in GOLDEN_TOTALS
           if got[q] != GOLDEN_TOTALS[q]}
    assert not bad, f"命中数（实测, 预期）不符：{bad}"


def test_empty_slot_and_question_mark_agree(js_all):
    """`[]` 与 `?` 是同一个东西：任意一个字。

    表单里它们是**两种卡片**（`?` 原文照写回，不回写成 `[]`），引擎里却必须完全
    等价——不等价的话，用户把 `?` 换成 `[]` 会悄悄换一套结果，而这两个写法看起来
    明明一样。两边各走一条匹配分支（AnyChar 与空 Slot），所以值得单独钉一条。
    """
    got = {q: js_all["search"][i]["total"]
           for i, (q, _o) in enumerate(SEARCH_JOBS) if q in ("?[~1]", "[][~1]")}
    assert set(got) == {"?[~1]", "[][~1]"}, "两条等价性用例都要留在 SEARCH_JOBS 里"
    assert got["?[~1]"] == got["[][~1]"], "同一个查询的两种写法给出了不同结果"


def test_single_char_readings_are_all_kept(corpus):
    """多音字：这个字有几套读音就收几套，任一套满足即命中。

    这是「加单字，读音标为歧义」那条决定的验收。单字**没有词级语境**——「行」在
    「银行」里是 háng、在「行走」里是 xíng，可它单独出现时无从判断——所以不挑不猜，
    全部并列收下。谁要是把读音截断到 5 套（客户端 flags 的逐套位只够 5 个），
    下面这几条会红：`那` 的第 6 套正是 nǎ。
    """
    by = {w.word: w for w in corpus.all_words}
    matcher = Matcher(corpus.chars)

    def hits(ch, q):
        return matcher.match(parse_pattern(q), by[ch]) is not None

    xing = by["行"]
    assert len(xing.reads) >= 5 and set(xing.read_sources) == {"char"}
    for q in ("[ang]", "[ing]", "[~2]"):
        assert hits("行", q), f"行 应当被 {q} 命中"

    na = by["那"]
    assert len(na.reads) > 5, "「那」有 10 套读音，正是超过逐套 flags 位的那批"
    assert hits("那", "[a,~3]") and hits("那", "[a,~4]")   # nǎ / nà 两套都要在

    for q in ("[uo]", "[u]"):
        assert hits("和", q), f"和 应当被 {q} 命中"

    # 反过来：不存在的读音不能被凑出来
    assert not hits("那", "[ang]")


def test_headline_query_counts_the_same_in_js(js_all):
    """README 的头号例子：JS 侧的总命中数也得是 1590。

    Python 侧的同名断言在 tests/test_engine.py 里（含那四个词的抽查）。这里只查
    总数，因为四个词是按词频排不进前 30 的，查不了。
    """
    js = js_all["search"][0]
    assert js["total"] == GOLDEN_TOTAL, f"JS 侧命中数变成 {js['total']}"
    dsl = js_all["search"][0]["dsl"]
    assert dsl == "[a,an,ang][~3,~4][7-][左右,独体]"


# ---------------------------------------------------------------------------
# 反向验证：这套比对本身得能发现差异
# ---------------------------------------------------------------------------

def test_diff_catches_a_changed_field():
    a = {"total": GOLDEN_TOTAL, "results": [{"word": "胆战心惊", "spans": [0, 1]}]}
    b = {"total": GOLDEN_TOTAL, "results": [{"word": "胆战心惊", "spans": [0, 2]}]}
    assert diff(a, b)
    assert "spans" in diff(a, b)


def test_diff_catches_a_missing_key():
    assert diff({"a": 1}, {"a": 1, "b": 2})
    assert diff({"a": 1, "b": 2}, {"a": 1})


def test_strip_timing_removes_only_the_clock():
    assert _strip_timing({"elapsed_ms": 1.0, "total": 2}) == {"total": 2}
    assert _strip_timing([{"elapsed_ms": 1}]) == [{}]
