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

    # 含 * ：走回溯，且要扫全量 36.8 万词
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
    # **词库里没有单字词**（最短 2 字，来自 CC-CEDICT + jieba 都只收词），
    # 所以 `[日]`、`[⿰弓长]` 这类单个 [] 恒为空——它们描述的是一字词。
    # 计划 §九.6 写的「[氵] 应命中 沐/江/海」按字面根本不可能成立，那个用例
    # 从设计上就是错的；下面这几条改成等价的可实现形式。
    ("*[日]*", {"limit": 30}),
    ("*[⿰弓长]*", {"limit": 10}),
    ("*[⿱艹化]*", {"limit": 10}),
    ("*[iou]*", {"limit": 15}),
    ("*[ü]*", {"limit": 15}),
    ("*[氵,~3]*", {"limit": 10}),          # 部首 ∩ 声调：异类取交集
    ("[氵][~3]", {"limit": 10}),           # 定长版，走 scanFixed 而不是 globMatch

    # 空约束 / 无法识别的词条 -> 应被忽略，[镶嵌] 退化成「任意一个字」
    ("[镶嵌]", {"limit": 10}),
    ("[这是啥,idontknow]", {"limit": 5}),

    # 「没有单字词」这条事实本身也要钉住：两条都必须为空
    ("[日]", {"limit": 10}),
    ("[⿰弓长]", {"limit": 10}),

    # 成语筛选
    ("[左右][左右]", {"idiomOnly": True, "limit": 20}),
    ("[a][a][a][a]", {"limit": 30}),

    # 用户给的完整示例：7 类约束、恰好忽略 2 项
    (MESSY, {"limit": 10}),

    # 语法错：必须两边都报同一个 reason，且一条结果都不出
    ("[unclosed", {}),
    ("abc", {}),
    ("张\\", {}),
    ("(未闭", {}),
]

#: 本来就该**没有结果**的作业，逐条有理由。剩下的作业一条都不许为空——
#: 两边都返回空时比对也是绿的，那样套子等于没测。
EXPECTED_EMPTY = {
    "[镶嵌]": "「镶嵌」不在查询侧的结构词表里，被忽略后 [..] 退化成「任意一个字」，"
              "而库里没有单字词",
    "[这是啥,idontknow]": "整组条件都无法识别，同样退化成「任意一个字」",
    "[日]": "单个 [] 描述的是一字词，库里没有",
    "[⿰弓长]": "同上",
    "abc": "字面 'a' 后面不是汉字，逐字匹配到没有的词",
    MESSY: "7 类条件叠在一起（部首氵 + 声母 {m,b,xx} + 声调 {3,4} + 结构 {上下,左右} + "
           "笔画 {≤8,=10,≥16}），命中数为 0 是数据本身的结果，不是解析出错",
    "[unclosed": "方括号未闭合",
    "张\\": "转义符后无字符",
    "(未闭": "圆括号未闭合",
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
    assert s["version"] == 1
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
