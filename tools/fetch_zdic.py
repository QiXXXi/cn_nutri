#!/usr/bin/env python3
"""抓取汉典（zdic.net）单字页的「字形结构」与「字形分析」。

为什么是汉典：IDS（表意文字描述序列）无法表达「独体字」——字统网与 makemeahanzi
都把 白/日/大/山/中/木 判成合成结构（见计划 §一）。汉典的「字形结构」字段是
少数直接给出 单一/左右/上下/半包围/全包围 分类的权威来源。字统网数据里
根本没有结构分类字段。

用法
----
    python -I tools/fetch_zdic.py --chars data_raw/corpus_chars.txt \
                                  --out data_raw/zdic \
                                  [--delay 0.25] [--probe 白日月]

带磁盘缓存：已抓过的字直接跳过，可随时中断续抓。
"""

from __future__ import annotations

import argparse
import gzip
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://www.zdic.net/hans/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 已实测的字段位置：<span class="meta-badge">标签</span> 后跟
# 一个 <span class="meta-value…">值</span>，或（部首）一个 <a class="meta-radical">字</a>。
RE_META = re.compile(
    r'<span class="meta-badge"[^>]*>\s*(?P<label>[^<]*?)\s*</span>\s*'
    r'(?:<a[^>]*class="meta-radical"[^>]*>(?P<radical>[^<]*)</a>'
    r'|<span class="meta-value[^"]*"[^>]*>(?P<value>[^<]*)</span>)',
    re.S,
)

# 结构标签归一：汉典写作「单一结构」，查询侧用「独体」
STRUCTURE_ALIASES = {
    "单一结构": "独体",
    "独体字": "独体",
    "左右结构": "左右",
    "上下结构": "上下",
    "左中右结构": "左中右",
    "上中下结构": "上中下",
    "半包围结构": "半包围",
    "全包围结构": "全包围",
    "镶嵌结构": "镶嵌",
    "品字结构": "品字",
}

# 六书
LIUSHU = ("象形", "指事", "会意", "形声", "转注", "假借")


def fetch(url: str, retries: int = 3, timeout: int = 20) -> str:
    """取一页 HTML，返回解码后的文本。

    汉典对**个别**字固定返回 504：实测「白」「张」0.1 秒就回来，而「书」每次都
    8 秒后 504——不是限流，是那一页在它自己的缓存里坏掉了。重试时带一个一次性
    查询串就能绕过缓存拿到 200，所以别干等，直接换 URL。
    """
    last: Exception | None = None
    for attempt in range(retries):
        target = url if attempt == 0 else f"{url}?r={int(time.time() * 1000)}"
        try:
            req = urllib.request.Request(
                target,
                headers={
                    "User-Agent": UA,
                    "Accept-Encoding": "gzip",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, gzip.BadGzipFile) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


def parse_page(page: str) -> dict:
    """从单字页里抽出结构 / 六书 / 部首 / 笔画。"""
    meta: dict[str, str] = {}
    for m in RE_META.finditer(page):
        label = html.unescape(m.group("label") or "").strip()
        value = html.unescape(m.group("radical") or m.group("value") or "").strip()
        if label and value and label not in meta:
            meta[label] = value

    raw_structure = meta.get("字形结构", "").strip()
    structure = STRUCTURE_ALIASES.get(raw_structure, raw_structure or None)

    analysis = meta.get("字形分析", "").strip()
    liushu = next((name for name in LIUSHU if name in analysis), None)

    strokes = None
    raw_strokes = meta.get("总笔画", "")
    if raw_strokes.isdigit():
        strokes = int(raw_strokes)

    radical = (meta.get("部首") or "").split("(")[0].strip() or None

    return {
        "structure": structure,
        "structure_raw": raw_structure or None,
        "liushu": liushu,
        "analysis": analysis or None,
        "radical_zdic": radical,
        "strokes_zdic": strokes,
        "found": bool(structure or liushu),
    }


def cache_path(outdir: str, ch: str) -> str:
    return os.path.join(outdir, f"u{ord(ch):05X}.json")


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取汉典单字字形结构")
    ap.add_argument("--chars", default="data_raw/corpus_chars.txt",
                    help="汉字表文件，每行一个（或单行连着写）")
    ap.add_argument("--out", default="data_raw/zdic", help="缓存目录")
    ap.add_argument("--delay", type=float, default=0.25, help="每次请求间隔秒")
    ap.add_argument("--probe", default=None,
                    help="只探测这几个字并打印结果，不写缓存")
    args = ap.parse_args()

    if args.probe:
        for ch in args.probe:
            url = BASE + urllib.parse.quote(ch)
            page = fetch(url)
            info = parse_page(page)
            print(f"{ch}  {url}")
            print(f"   {json.dumps(info, ensure_ascii=False)}")
        return 0

    with open(args.chars, encoding="utf-8") as fh:
        chars = sorted({c for c in fh.read() if not c.isspace()})
    os.makedirs(args.out, exist_ok=True)

    todo = [c for c in chars if not os.path.exists(cache_path(args.out, c))]
    print(f"共 {len(chars)} 字，已缓存 {len(chars) - len(todo)}，待抓 {len(todo)}",
          flush=True)
    if not todo:
        return 0

    t0 = time.time()
    done = 0
    failed: list[str] = []
    for ch in todo:
        try:
            info = parse_page(fetch(BASE + urllib.parse.quote(ch)))
            info["char"] = ch
            with open(cache_path(args.out, ch), "w", encoding="utf-8") as fh:
                json.dump(info, fh, ensure_ascii=False)
            if not info["found"]:
                failed.append(ch)
        except Exception as exc:  # 单字失败不该中断整轮
            print(f"  !! {ch}: {exc}", flush=True)
            failed.append(ch)
        done += 1
        if done % 50 == 0 or done == len(todo):
            rate = done / max(time.time() - t0, 1e-6)
            eta = (len(todo) - done) / max(rate, 1e-6)
            print(f"  {done}/{len(todo)}  {rate:.2f} 字/秒  剩余约 {eta/60:.1f} 分钟"
                  f"  无数据 {len(failed)}", flush=True)
        time.sleep(args.delay)

    print(f"完成。无结构/六书数据的字 {len(failed)} 个，前 20：{''.join(failed[:20])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
