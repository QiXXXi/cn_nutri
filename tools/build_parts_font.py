#!/usr/bin/env python3
"""把网页面板里的 179 个「难输入部件」做成一个内置字体。

为什么不继续用 SVG
------------------
字统网给这 179 个部件事先准备了骨架线 SVG，正因为系统字体普遍没有它们的字形。
但 SVG 只能画形状：不能选中、不能复制、不随字号缩放、不参与文本排版，跟旁边的
真字摆在一起时基线也对不齐。做成真字形就没这些问题。

为什么不是一个字体
------------------
这 179 个里只有 8 个在基本区（龰龱龴龵龶龷龸龹，U+9FB0–9FB9），其余 171 个都在
增补平面。实测各家字体的覆盖：

    思源黑体 Source Han Sans SC     19/179   ← 扩展 B 一个都没有，光它不够
    遍黑体 Plangothic P1           155/179
    遍黑体 Plangothic P2            32/179
    三个合起来                      179/179

思源黑体与遍黑体正好互补：遍黑体本来就是为「补思源黑体没有的字」而做的，而它缺
的 8 个基本区尾字思源黑体有。两者都是 SIL OFL 1.1，都允许改写与再分发。

所以按 遍黑体P1 → 遍黑体P2 → 思源黑体 的顺序，给每个部件挑第一个覆盖它的字体，
各自子集化到只剩自己那份字形。三个文件合计约 31 KB，比原来的 SVG 数据（254 KB）
还小，而且是真文字。

许可证
------
改动字体即触发 OFL 的「保留字体名」条款（思源黑体保留了 `Source`），所以子集一律
改名成 `cn-nutri-parts`；版权声明与许可证原文照留在字体文件内，另存一份在输出目录。

用法
----
    python -I tools/build_parts_font.py [--raw data_raw] [--out src/cn_nutri/web/fonts]

源字体缓存在 ``data_raw/fonts/``（约 41 MB，只在需要重新生成时才下载）。
需要 ``fontTools``（``pip install fonttools``）——**只有本脚本需要**，运行时依旧是零依赖。
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

#: 子集后的统一字体名（必须避开思源黑体保留的 `Source`）
FAMILY = "cn-nutri-parts"

UA = "cn-nutri/build_parts_font"

#: (内部名, 缓存文件名, 下载地址, 许可证文本地址)
SOURCES = [
    (
        "plangothic-1",
        "PlangothicP1-Regular.ttf",
        "https://github.com/Fitzgerald-Porthmouth-Koenigsegg/Plangothic_Project"
        "/releases/download/V2.9.5795/PlangothicP1-Regular.ttf",
        "https://cdn.jsdelivr.net/gh/Fitzgerald-Porthmouth-Koenigsegg"
        "/Plangothic_Project@main/LICENSE-OFL.txt",
    ),
    (
        "plangothic-2",
        "PlangothicP2-Regular.ttf",
        "https://github.com/Fitzgerald-Porthmouth-Koenigsegg/Plangothic_Project"
        "/releases/download/V2.9.5795/PlangothicP2-Regular.ttf",
        "https://cdn.jsdelivr.net/gh/Fitzgerald-Porthmouth-Koenigsegg"
        "/Plangothic_Project@main/LICENSE-OFL.txt",
    ),
    (
        "source-han-sans-sc",
        "SourceHanSansCN-Regular.otf",
        "https://cdn.jsdelivr.net/gh/adobe-fonts/source-han-sans@release"
        "/SubsetOTF/CN/SourceHanSansCN-Regular.otf",
        "https://cdn.jsdelivr.net/gh/adobe-fonts/source-han-sans@release/LICENSE.txt",
    ),
]

#: 子集文件按分配顺序命名；CSS 里三个 @font-face 共用 FAMILY，靠 unicode-range 分流
SLOT_NAMES = ["parts-a", "parts-b", "parts-c"]


def download(url: str, dest: str) -> None:
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  已缓存 {dest}")
        return
    print(f"  下载 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(raw)
    print(f"    -> {len(raw)/1e6:.1f} MB")


def unicode_range(codepoints) -> str:
    """把码位列表压成 CSS ``unicode-range``：连续段写成 ``U+XXXX-YYYY``。"""
    out, start, prev = [], None, None
    for cp in sorted(codepoints):
        if start is None:
            start = prev = cp
        elif cp == prev + 1:
            prev = cp
        else:
            out.append(f"U+{start:X}" if start == prev else f"U+{start:X}-{prev:X}")
            start = prev = cp
    if start is not None:
        out.append(f"U+{start:X}" if start == prev else f"U+{start:X}-{prev:X}")
    return ",".join(out)


def rename(font, family: str) -> None:
    """把子集改名，并保留版权/许可证/商标三条记录（OFL 要求原样保留）。"""
    for rec in font["name"].names:
        if rec.nameID in (1, 16):          # 字体族 / 排版族名
            rec.string = family
        elif rec.nameID == 4:              # 全名
            rec.string = family
        elif rec.nameID == 6:              # PostScript 名（不能有空格）
            rec.string = family.replace(" ", "-")
        elif rec.nameID == 3:              # 唯一标识
            rec.string = family
    if "CFF " in font:
        font["CFF "].cff.fontNames = [family.replace(" ", "-")]


def main() -> int:
    ap = argparse.ArgumentParser(description="生成难输入部件的内置字体")
    ap.add_argument("--raw", default="data_raw", help="原始数据目录")
    ap.add_argument("--out", default=os.path.join("src", "cn_nutri", "web", "fonts"),
                    help="输出目录")
    ap.add_argument("--components", default=None,
                    help="难输入部件 JSON（默认 <raw>/zitools/_difficult_components.json）")
    args = ap.parse_args()

    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError:
        print("需要 fontTools：pip install fonttools", file=sys.stderr)
        return 1

    src_path = args.components or os.path.join(
        args.raw, "zitools", "_difficult_components.json")
    with open(src_path, encoding="utf-8") as fh:
        data = json.load(fh)
    targets = [c for c in (data.get("font") or {}) if c != "_page_count"]
    print(f"部件 {len(targets)} 个，来自 {src_path}")

    cache = os.path.join(args.raw, "fonts")
    fonts, cmaps = [], {}
    for name, filename, url, _license in SOURCES:
        path = os.path.join(cache, filename)
        download(url, path)
        font = TTFont(path, lazy=True)
        cmap = set()
        for table in font["cmap"].tables:
            cmap |= set(table.cmap.keys())
        font.close()
        fonts.append((name, path))
        cmaps[name] = cmap

    # 按顺序把每个部件分给第一个覆盖它的字体
    assign: dict[str, list[str]] = {name: [] for name, _ in fonts}
    for ch in targets:
        for name, _ in fonts:
            if ord(ch) in cmaps[name]:
                assign[name].append(ch)
                break
    missing = [c for c in targets if not any(ord(c) in cmaps[n] for n, _ in fonts)]
    if missing:
        print(f"警告：{len(missing)} 个部件没有任何源字体覆盖：{''.join(missing[:20])}")

    os.makedirs(args.out, exist_ok=True)
    options = subset.Options()
    options.layout_features = []      # 这些字形不需要任何 OpenType 布局特性
    options.hinting = False
    # 默认只留 nameID 0–6，会把许可证（13）和商标（7）丢掉。OFL 要求这两条随
    # 字体一起走，得显式全留。
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.desubroutinize = True     # CFF 子集化后体积更小
    options.drop_tables += ["DSIG", "meta", "STAT", "fvar", "gvar", "HVAR", "MVAR"]

    faces = []
    for slot, (name, path) in zip(SLOT_NAMES, fonts):
        chars = assign[name]
        if not chars:
            continue
        font = subset.load_font(path, options)
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(text="".join(chars))
        subsetter.subset(font)
        rename(font, FAMILY)
        ext = ".otf" if "CFF " in font else ".ttf"
        out = os.path.join(args.out, slot + ext)
        subset.save_font(font, out, options)
        font.close()
        size = os.path.getsize(out)
        print(f"  {slot}{ext}  {len(chars):3d} 字形  {size/1024:.1f} KB  <- {name}")
        faces.append((slot, ext, [ord(c) for c in chars]))

    css = ["/* 由 tools/build_parts_font.py 生成，请勿手改。 */"]
    for slot, ext, cps in faces:
        fmt = "opentype" if ext == ".otf" else "truetype"
        css.append(
            f'@font-face{{font-family:"{FAMILY}";font-display:swap;'
            f'src:url("{slot}{ext}") format("{fmt}");'
            f"unicode-range:{unicode_range(cps)}}}")
    css_path = os.path.join(args.out, "parts.css")
    with open(css_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(css) + "\n")
    print(f"  parts.css  {os.path.getsize(css_path)/1024:.1f} KB")

    # 许可证原文：OFL 要求随字体一起分发
    for _name, _filename, _url, license_url in SOURCES:
        if not license_url:
            continue
        lic = "Plangothic" if "Plangothic" in license_url else "SourceHanSans"
        dest = os.path.join(args.out, f"LICENSE-OFL-{lic}.txt")
        try:
            download(license_url, dest)
        except Exception as exc:
            print(f"  许可证下载失败（{license_url}）：{exc}")

    total = sum(os.path.getsize(os.path.join(args.out, s + e)) for s, e, _ in faces)
    print(f"\n合计 {total/1024:.1f} KB，输出到 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
