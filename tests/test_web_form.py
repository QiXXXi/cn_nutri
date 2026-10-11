"""逐字表单的状态机回归：表达式 <-> 表单的双向同步、暂停与恢复。

这个模块只干一件事——把**页面里那段内联脚本**真的跑起来（[tools/form_cli.js][] 给它
垫了一层最小 DOM），然后按用户的操作顺序驱动它，看状态对不对。抄一份逻辑出来测
是不行的：页面改了、抄件没改，套子照样绿。

覆盖的坑是「点得到的按钮把表单从暂停状态里放出来」：

    表达式有语法错、或者字面不是汉字时，表单会**暂停同步**（输入框禁用）。此时用户点
    「清空」「+ 加一个字位」「+ 任意多个字」——表达式已经被换成普通式子，表单却一直是
    灰的，而且没有入口能把它唤醒，因为表单的输入框自己就是禁用的。

`?` 与 `*` **不再**是暂停原因：它们各有一张卡片（`any` / `star`），表达式原文写回时
一个字都不用改。这里顺带钉住卡片标题的字序换算——`*` 不占字，它后面的字位只能标
「第 N 字起」，这是新写的逻辑，也是「卡片序号 ≠ 字序」这句话的唯一体现。

看不到的是逐个 <input> 的 disabled：那些元素是 renderSlots 用 innerHTML 字符串
拼出来的，垫的 DOM 里没有真元素。这里观察 formLocked、#form-note 文案、表达式
文本、卡片标题与提示区 HTML——renderSlots 的 disabled 就是从 formLocked 一行推出来的。

要用 node。没装 node 就整模块 skip——**不是通过**，是没验。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "src", "cn_nutri", "web")
CLI = os.path.join(ROOT, "tools", "form_cli.js")


def _find_node() -> str | None:
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
    pytest.skip("没有 node：逐字表单驱动不起来（winget install OpenJS.NodeJS.LTS）",
                allow_module_level=True)


# ---------------------------------------------------------------------------
# 操作序列
# ---------------------------------------------------------------------------
#
# 每条是一个用户动作，跑完记一次状态。挑的都是能压到不同状态的：
# 两种暂停原因（语法错、字面非汉字）各配一条出路（清空、加字位、加星号卡），
# `?`/`*` 卡片的往返与相邻星号的死按钮，以及表单自己**写出**非法表达式
# （≠A 没有配对的 =A）时**不该**暂停。

STEPS: list[dict] = [
    {"set": "*[氵]*"},                              #  0 不再暂停：* 有自己的卡片
    {"click": "#clear-slots"},                      #  1
    {"click": "#add-slot"},                         #  2
    {"click": "#add-star"},                         #  3
    {"click": "#add-star"},                         #  4 ★ 末尾已是 *，这次必须什么也不做
    {"click": "#add-any"},                          #  5
    {"set": "?[~1]"},                               #  6 不再暂停：? 有自己的卡片
    {"click": "#clear-slots"},                      #  7
    {"set": "张\\"},                                 #  8 暂停：语法错
    {"click": "#clear-slots"},                      #  9 ★ 出路一
    {"set": "abc"},                                 # 10 暂停：字面 a 不是汉字
    {"click": "#add-slot"},                         # 11 ★ 出路二
    {"click": "#add-star"},                         # 12
    {"set": ""},                                    # 13 表达式清空
    {"set": "*[日]*"},                              # 14 星号后面的字位只能标「起」
    {"click": "#clear-slots"},                      # 15
    {"type": {"i": 0, "k": "literals", "v": "张白"}},   # 16 粘贴两字，按码位截断
    {"type": {"i": 0, "k": "marks", "v": "=A"}},        # 17
    {"type": {"i": 0, "k": "marks", "v": "≠A"}},        # 18 表单写出非法式子
    {"type": {"i": 0, "k": "marks", "v": "!=A, =B"}},   # 19 != 别名 + 大小写与空白
    {"click": "#clear-slots"},                      # 20
    {"set": "abc"},                                 # 21 表单里已经有内容时再锁一次
    {"click": "#add-star"},                         # 22 ★ 出路三
    {"click": "#clear-slots"},                      # 23
    {"type": {"i": 0, "k": "subsets", "v": "姓氏"}},      # 24 子集就是一个字位条件，卡片不改种类
    {"type": {"i": 0, "k": "subsets", "v": "姓氏,朝代"}}, # 25 两个子集：组内取并集
    {"click": "#clear-slots"},                      # 26
    {"set": "[$姓氏][][][]"},                       # 27 表达式 -> 表单：子集要能回填
    {"type": {"i": 1, "k": "subsets", "v": "朝代"}},      # 28 回填后再改第 2 字
    {"click": "#clear-slots"},                      # 29
    {"set": "[$姓氐]"},                             # 30 子集名不认识：整条式子报错 -> 暂停
    {"click": "#clear-slots"},                      # 31 ★ 出路还是清空
]

#: (locked, 表达式, 表单备注应含, 提示区应含, 卡片标题)。备注与提示区都写子串，
#: 空串表示**必须为空**。
#:
#: 提示区那条最要紧：它必须**跟着表达式走**——`?`/`*` 不再锁表单之后，「解除暂停时
#: 顺手补一次」那条路径没了，清空之后会留着上一步的旧话骗人。而**表单自己**写出的
#: `[≠A]`（用户敲了 ≠A 却没敲 =A）会如实报语法错：它只提示、不暂停，见最后一条测试。
EXPECTED: list[tuple[bool, str, str, str, list[str]]] = [
    (False, "*[氵]*", "", "含 <code>*</code>",
     ["任意多个字（*）", "第 1 字起", "任意多个字（*）"]),
    (False, "[]", "", "匹配 1 字词", ["第 1 字 · 任意"]),
    (False, "[][]", "", "匹配 2 字词", ["第 1 字 · 任意", "第 2 字 · 任意"]),
    (False, "[][]*", "", "含 <code>*</code>",
     ["第 1 字 · 任意", "第 2 字 · 任意", "任意多个字（*）"]),
    (False, "[][]*", "", "含 <code>*</code>",
     ["第 1 字 · 任意", "第 2 字 · 任意", "任意多个字（*）"]),
    (False, "[][]*?", "", "含 <code>*</code>",
     ["第 1 字 · 任意", "第 2 字 · 任意", "任意多个字（*）", "第 3 字起 · 任意一字"]),
    (False, "?[~1]", "", "匹配 2 字词", ["第 1 字 · 任意一字", "第 2 字"]),
    (False, "[]", "", "匹配 1 字词", ["第 1 字 · 任意"]),
    (True,  "张\\", "语法错误", "语法错误", ["第 1 字 · 任意"]),
    (False, "[]", "", "匹配 1 字词", ["第 1 字 · 任意"]),
    (True,  "abc", "不是汉字", "匹配 3 字词", ["第 1 字 · 任意"]),
    (False, "[][]", "", "匹配 2 字词", ["第 1 字 · 任意", "第 2 字 · 任意"]),
    (False, "[][]*", "", "含 <code>*</code>",
     ["第 1 字 · 任意", "第 2 字 · 任意", "任意多个字（*）"]),
    (False, "", "", "", ["第 1 字 · 任意"]),
    (False, "*[日]*", "", "含 <code>*</code>",
     ["任意多个字（*）", "第 1 字起", "任意多个字（*）"]),
    (False, "[]", "", "匹配 1 字词", ["第 1 字 · 任意"]),
    (False, "[(张)]", "", "匹配 1 字词", ["第 1 字"]),
    (False, "[=A,(张)]", "", "匹配 1 字词", ["第 1 字"]),
    (False, "[≠A,(张)]", "", "语法错误", ["第 1 字"]),
    (False, "[=B,≠A,(张)]", "", "语法错误", ["第 1 字"]),
    (False, "[]", "", "匹配 1 字词", ["第 1 字 · 任意"]),
    (True,  "abc", "不是汉字", "匹配 3 字词", ["第 1 字 · 任意"]),
    (False, "[]*", "", "含 <code>*</code>",
     ["第 1 字 · 任意", "任意多个字（*）"]),
    (False, "[]", "", "匹配 1 字词", ["第 1 字 · 任意"]),
    (False, "[$姓氏]", "", "匹配 1 字词", ["第 1 字"]),
    (False, "[$姓氏,$朝代]", "", "匹配 1 字词", ["第 1 字"]),
    (False, "[]", "", "匹配 1 字词", ["第 1 字 · 任意"]),
    (False, "[$姓氏][][][]", "", "匹配 4 字词",
     ["第 1 字", "第 2 字 · 任意", "第 3 字 · 任意", "第 4 字 · 任意"]),
    (False, "[$姓氏][$朝代][][]", "", "匹配 4 字词",
     ["第 1 字", "第 2 字", "第 3 字 · 任意", "第 4 字 · 任意"]),
    (False, "[]", "", "匹配 1 字词", ["第 1 字 · 任意"]),
    (True,  "[$姓氐]", "语法错误", "语法错误", ["第 1 字 · 任意"]),
    (False, "[]", "", "匹配 1 字词", ["第 1 字 · 任意"]),
]

assert len(STEPS) == len(EXPECTED), "步骤与预期条数对不上"


@pytest.fixture(scope="module")
def log() -> list[dict]:
    """一次进程跑完全部步骤——每步都要解码 12.9 MB 词库，一条一条跑太慢。"""
    payload = json.dumps(STEPS, ensure_ascii=False).encode("utf-8")
    proc = subprocess.run([NODE, CLI, WEB], input=payload,
                          capture_output=True, timeout=300)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[:4000]
    out = json.loads(proc.stdout.decode("utf-8"))
    assert len(out) == len(STEPS), "状态条数与步骤数对不上"
    return out


@pytest.mark.parametrize("i", range(len(STEPS)), ids=[str(s) for s in STEPS])
def test_form_state_after_each_step(log, i):
    locked, dsl, note, notice, labels = EXPECTED[i]
    got = log[i]
    assert got["locked"] is locked, f"暂停状态不对：{got['locked']}"
    assert got["dsl"] == dsl, f"表达式不对：{got['dsl']!r}"
    if note:
        assert note in got["note"], f"表单备注不对：{got['note']!r}"
    else:
        assert got["note"] == "", f"表单备注应当为空：{got['note']!r}"
    assert notice in got["notice"], f"提示区不对：{got['notice']!r}"
    assert got["labels"] == labels, f"卡片标题不对：{got['labels']!r}"


def test_star_cards_refuse_to_be_adjacent(log):
    """★ 第 4 步：末尾已经是 `*` 卡时再点「+ 任意多个字」，必须一动不动。

    放行的话表单会画出两张 `*` 卡片，而解析器把 `**` 折成**一个** `*`
    （query.py 的 parse），于是卡片数与表达式对不上，之后每一次同步都在骗人。
    """
    assert log[3]["dsl"] == "[][]*"
    assert log[4]["dsl"] == log[3]["dsl"], "相邻星号卡片不该被加进来"
    assert log[4]["kinds"] == log[3]["kinds"]
    assert log[4]["addStar"] is True, "按钮应当自报禁用"
    # 删掉那两张槽位后末尾不再是星号，按钮要活过来
    assert log[15]["dsl"] == "[]" and log[15]["addStar"] is False


def test_question_mark_survives_the_round_trip(log):
    """`?` 是独立卡片，表达式原文一个字都不改——不回写成 `[]`。

    `?` 与 `[]` 在引擎里等价（test_client_engine 有对拍），但在**表单**里必须是两种
    卡片：回写成 `[]` 就等于替用户改了他的表达式，而他很可能正想用 `?` 表示
    「这里随便什么字，别管」。
    """
    assert log[6]["dsl"] == "?[~1]"
    assert log[6]["kinds"] == ["any", "slot"]


def test_the_form_can_always_release_the_lock(log):
    """把「表单能自救」这条单独钉一遍：**两个**暂停原因，**三条**出路。

    上面那条逐条断言已经覆盖了，这条是**反着**说的——哪天有人给 syncFromForm
    加回「只在无错时才解锁」之类的条件，逐条断言会一起红，但红在哪不好看出来。
    """
    for paused, released in ((8, 9), (10, 11), (21, 22)):
        assert log[paused]["locked"] is True, f"第 {paused} 步应当暂停"
        assert log[paused]["notice"], f"第 {paused} 步应当有提示"
        assert log[released]["locked"] is False, f"第 {released} 步应当被放出来"
        assert log[released]["note"] == ""
        # 提示区描述的是**表达式**，出路换了一条新表达式，它就必须重算——留着上一步
        # 那条「语法错误」「不是汉字」的旧话，就是在骗人。
        assert log[released]["notice"] != log[paused]["notice"], "提示区留着暂停时那条旧话"


def test_form_side_never_pauses_on_its_own_error(log):
    """表单写出非法表达式时**只提示、不暂停**。

    `[≠A]` 本身是语法错，但它是用户在表单里敲出来的——要是这时候把表单禁掉，
    用户就再也改不回来了（输入框自己就是禁用的）。所以暂停只能由表达式那一侧发起。
    """
    assert log[18]["dsl"] == "[≠A,(张)]"
    assert log[18]["locked"] is False
    assert log[18]["note"] == ""
    # 它确实非法，提示区也如实这么说——只是**不**拿它去禁用表单
    assert "语法错误" in log[18]["notice"]
