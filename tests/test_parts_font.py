"""部件面板：分组顺序 + 内置字体的覆盖。

这些用例只依赖构建产物（``data_raw/zitools/_difficult_components.json`` 与
``src/cn_nutri/web/fonts/``），不需要词库，所以在没构建过 DB 的环境里照样跑。

字体覆盖不靠 fontTools 复算，而是直接读生成的 ``parts.css``，把 ``unicode-range``
展开来比对——真正决定浏览器会不会画出豆腐块的就是那串 range，验它比验字体文件本身
更贴近实际。
"""

from __future__ import annotations

import os
import re

import pytest

import json

from cn_nutri.components import DEFAULT_RAW, load_components, read_raw
from cn_nutri.server import WEB_DIR

FONTS_DIR = os.path.join(WEB_DIR, "fonts")
CSS_PATH = os.path.join(FONTS_DIR, "parts.css")

#: 需要 data_raw 的用例单独把住——仓库里不带 data_raw/，但带烘好的
#: web/data/components.json，所以分组类的用例在刚 clone 的环境里也该跑得起来。
needs_raw = pytest.mark.skipif(
    not os.path.exists(DEFAULT_RAW),
    reason="没有字统网部件表：先跑 python -I tools/build_db.py")


def _difficult_components() -> list[str]:
    """有 SVG 骨架线的那 179 个——也就是系统字体普遍没有字形的那批。"""
    return [c for c in ((read_raw() or {}).get("font") or {}) if c != "_page_count"]


def _css_codepoints(css: str) -> set[int]:
    out: set[int] = set()
    for block in re.findall(r"@font-face\s*\{[^}]*\}", css):
        m = re.search(r"unicode-range\s*:\s*([^;}]+)", block)
        if not m:
            continue
        for part in m.group(1).split(","):
            part = part.strip()
            if not part.upper().startswith("U+"):
                continue
            ends = part[2:].split("-")
            lo = int(ends[0], 16)
            hi = int(ends[1], 16) if len(ends) > 1 else lo
            out.update(range(lo, hi + 1))
    return out


# ------------------------------------------------------------------ 分组顺序

def test_groups_are_sorted_by_stroke_count():
    """分组键是字符串，"10" 的字典序在 "2" 前面——必须按数值排。"""
    groups = load_components()["groups"]
    labels = [g["label"] for g in groups]

    assert labels[:2] == ["常用偏旁", "康熙部首"], labels[:2]

    # 键有 "1"…"12" 和 "13+"；"13+" 排最后，取前导数字比对
    strokes = [int(re.match(r"\d+", lab).group()) for lab in labels[2:]]
    assert strokes == sorted(strokes), f"按笔画分组顺序不对：{labels[2:]}"
    assert strokes[0] == 1, f"笔画分组应从 1 画起，实际从 {strokes[0]} 画起"
    assert labels[-1] == "13 画以上", labels[-1]


def test_every_group_is_non_empty():
    for g in load_components()["groups"]:
        assert g["chars"], f"空分组：{g['label']}"


def test_components_payload_no_longer_ships_outlines():
    """字形改由内置字体渲染，接口不该再把 SVG 路径塞进响应。"""
    assert "svg" not in load_components()


# ------------------------------------------------------------------ 字体覆盖

@needs_raw
def test_bundled_font_covers_every_difficult_component():
    if not os.path.exists(CSS_PATH):
        pytest.skip("还没生成内置字体：python -I tools/build_parts_font.py")

    with open(CSS_PATH, encoding="utf-8") as fh:
        covered = _css_codepoints(fh.read())

    targets = _difficult_components()
    assert len(targets) > 150, f"部件表看起来不对：只有 {len(targets)} 个"

    missing = [c for c in targets if ord(c) not in covered]
    assert not missing, (
        f"{len(missing)} 个部件不在内置字体的 unicode-range 里，"
        f"页面上会是豆腐块：{''.join(missing[:30])}")


def test_bundled_font_files_exist():
    if not os.path.exists(CSS_PATH):
        pytest.skip("还没生成内置字体：python -I tools/build_parts_font.py")

    with open(CSS_PATH, encoding="utf-8") as fh:
        css = fh.read()

    urls = re.findall(r'url\("([^"]+)"\)', css)
    assert urls, "parts.css 里没有任何 @font-face"
    for url in urls:
        assert os.path.isfile(os.path.join(FONTS_DIR, url)), f"缺字体文件 {url}"


def test_bundled_font_is_small():
    """内置字体加起来得明显小于它替换掉的 SVG 数据（254 KB）。"""
    if not os.path.isdir(FONTS_DIR):
        pytest.skip("还没生成内置字体")
    total = sum(os.path.getsize(os.path.join(FONTS_DIR, f))
                for f in os.listdir(FONTS_DIR)
                if f.endswith((".ttf", ".otf")))
    assert 0 < total < 150 * 1024, f"内置字体 {total/1024:.0f} KB，太大了"
