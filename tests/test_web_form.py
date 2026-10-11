"""逐字表单的状态机回归：表达式 <-> 表单的双向同步、暂停与恢复。

这个模块只干一件事——把**页面里那段内联脚本**真的跑起来（[tools/form_cli.js][] 给它
垫了一层最小 DOM），然后按用户的操作顺序驱动它，看状态对不对。抄一份逻辑出来测
是不行的：页面改了、抄件没改，套子照样绿。

覆盖的坑是「点得到的按钮把表单从暂停状态里放出来」：

    表达式含 * 或 ? 时表单会**暂停同步**（输入框禁用）。此时用户点「清空」或
    「+ 加一个字位」——表达式已经被换成普通式子，表单却一直是灰的，而且没有入口
    能把它唤醒，因为表单的输入框自己就是禁用的。

看不到的是逐个 <input> 的 disabled：那些元素是 renderSlots 用 innerHTML 字符串
拼出来的，垫的 DOM 里没有真元素。这里观察 formLocked、#form-note 文案、表达式
文本与提示区 HTML——renderSlots 的 disabled 就是从 formLocked 一行推出来的。

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
# 含 * / 含 ? / 语法错 三种暂停原因，加字位与清空两条出路，
# 以及表单自己**写出**非法表达式（≠A 没有配对的 =A）时**不该**暂停。

STEPS: list[dict] = [
    {"set": "*[氵]*"},                              # 0 暂停：* 表单表示不了
    {"click": "#clear-slots"},                      # 1 ★ 清空 -> 必须恢复
    {"click": "#add-slot"},                         # 2 ★ 加字位 -> 必须恢复
    {"set": ""},                                    # 3 表达式清空
    {"set": "?[~1]"},                               # 4 暂停：? 同样表示不了
    {"click": "#clear-slots"},                      # 5 ★
    {"set": "张\\"},                                 # 6 暂停：语法错
    {"click": "#clear-slots"},                      # 7 ★
    {"type": {"i": 0, "k": "literals", "v": "张白"}},   # 8 粘贴两字，按码位截断
    {"type": {"i": 0, "k": "marks", "v": "=A"}},        # 9
    {"type": {"i": 0, "k": "marks", "v": "≠A"}},        # 10 表单写出非法式子
    {"type": {"i": 0, "k": "marks", "v": "!=A, =B"}},   # 11 != 别名 + 大小写与空白
]

#: (locked, 表达式, 表单备注应为空, 提示区应包含)。提示区那条最要紧：解除暂停时
#: 必须**重算**，否则会留着上一步「该表达式含 ? 或 *」的旧话去骗人。
EXPECTED: list[tuple[bool, str, bool, str]] = [
    (True,  "*[氵]*",       False, "含 <code>*</code>"),
    (False, "[]",           True,  "匹配 1 字词"),
    (False, "[][]",         True,  "匹配 1 字词"),
    (False, "",             True,  ""),
    (True,  "?[~1]",        False, "匹配 2 字词"),
    (False, "[]",           True,  "匹配 1 字词"),
    (True,  "张\\",          False, "语法错误"),
    (False, "[]",           True,  "匹配 1 字词"),
    (False, "[(张)]",       True,  "匹配 1 字词"),
    (False, "[=A,(张)]",    True,  "匹配 1 字词"),
    (False, "[≠A,(张)]",    True,  "匹配 1 字词"),
    (False, "[=B,≠A,(张)]", True,  "匹配 1 字词"),
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
    locked, dsl, note_empty, notice = EXPECTED[i]
    got = log[i]
    assert got["locked"] is locked, f"暂停状态不对：{got['locked']}"
    assert got["dsl"] == dsl, f"表达式不对：{got['dsl']!r}"
    assert bool(got["note"]) is not note_empty, f"表单备注不对：{got['note']!r}"
    assert notice in got["notice"], f"提示区不对：{got['notice']!r}"


def test_the_form_can_always_release_the_lock(log):
    """把「表单能自救」这条单独钉一遍：三个暂停原因各配一个出路。

    上面那条逐条断言已经覆盖了，这条是**反着**说的——哪天有人给 syncFromForm
    加回「只在无错时才解锁」之类的条件，逐条断言会一起红，但红在哪不好看出来。
    """
    for paused, released in ((0, 1), (4, 5), (6, 7)):
        assert log[paused]["locked"] is True
        assert log[released]["locked"] is False
        assert log[released]["note"] == ""
        assert "含 <code>*</code>" not in log[released]["notice"]
        assert "语法错误" not in log[released]["notice"]


def test_form_side_never_pauses_on_its_own_error(log):
    """表单写出非法表达式时**只提示、不暂停**。

    `[≠A]` 本身是语法错，但它是用户在表单里敲出来的——要是这时候把表单禁掉，
    用户就再也改不回来了（输入框自己就是禁用的）。所以暂停只能由表达式那一侧发起。
    """
    assert log[10]["dsl"] == "[≠A,(张)]"
    assert log[10]["locked"] is False
    assert log[10]["note"] == ""
