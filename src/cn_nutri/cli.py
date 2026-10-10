"""命令行入口。

    python -m cn_nutri "[a,an,ang][~3,~4][7-][独体,左右]" --limit 50
"""

from __future__ import annotations

import argparse
import sys

from .corpus import DEFAULT_DB, load_corpus
from .search import search

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

__all__ = ["main"]

_C = {
    "dim": "\033[2m", "bold": "\033[1m", "cy": "\033[36m",
    "yl": "\033[33m", "gr": "\033[32m", "reset": "\033[0m",
}


def _c(text: str, color: str, enabled: bool) -> str:
    return f"{_C[color]}{text}{_C['reset']}" if enabled else text


def _describe(match, corpus, mark: bool) -> str:
    w = match.word
    pys = w.pinyin_display
    if w.reads and 0 <= match.read_index < len(pys):
        py = pys[match.read_index]
        src = w.read_sources[match.read_index] if match.read_index < len(w.read_sources) else ""
        if src == "inferred":
            py += "  ?"
    else:
        py = ""

    info = [f"{len(w.word)}字"]
    if w.freq:
        info.append(f"频{w.freq}")
    if w.is_idiom:
        info.append("成语")
    structs = [corpus.chars[c].structure for c in w.word if c in corpus.chars]
    if any(structs):
        info.append("/".join(s or "?" for s in structs))

    line = f"{_c(w.word, 'bold', mark)}  {_c(py, 'cy', mark)}"
    line += f"  {_c(' '.join(info), 'dim', mark)}"
    if w.definition:
        line += f"  {_c(w.definition[:60], 'dim', mark)}"
    return line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cn_nutri",
        description="按「逐字属性」反查汉语词语。示例："
                    "cn_nutri \"[a,an,ang][~3,~4][7-][独体,左右]\"")
    ap.add_argument("query", nargs="?", help="DSL 查询；省略则进入交互模式")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--sort", default="freq",
                    choices=["freq", "len", "strokes", "pinyin"])
    ap.add_argument("--idiom", action="store_true", help="只要成语")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--stats", action="store_true", help="显示耗时与扫描量")
    args = ap.parse_args(argv)

    mark = not args.no_color and sys.stdout.isatty()
    corpus = load_corpus(args.db)

    def run(q: str) -> None:
        res = search(corpus, q, limit=args.limit, offset=args.offset,
                     sort=args.sort, idiom_only=args.idiom)
        if res.error:
            print(_c(f"语法错误：{res.error}", "yl", mark))
            for e in res.errors:
                print(f"  偏移 {e.offset}: {e.text}")
            return
        if res.ignored:
            print(_c("忽略的条件：" + "、".join(i.text for i in res.ignored),
                     "yl", mark))
        print(f"命中 {_c(str(res.total), 'gr', mark)} 条"
              f"（显示第 {res.offset + 1}–{res.offset + len(res.matches)} 条）")
        for m in res.matches:
            print("  " + _describe(m, corpus, mark))
        if res.truncated:
            print(_c(f"结果超过上限，请收窄条件", "dim", mark))
        if res.stats.excluded_missing:
            print(_c(f"另有 {res.stats.excluded_missing} 个词因字形数据缺失"
                     f"无法判定而排除：{''.join(sorted(res.stats.missing_chars)[:30])}",
                     "dim", mark))
        if args.stats:
            print(_c(f"[{res.elapsed_ms:.0f} ms, 扫描 {res.stats.scanned} 词]",
                     "dim", mark))

    if args.query:
        run(args.query)
        return 0

    print("cn_nutri 交互模式（Ctrl-D 退出）")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if q:
            run(q)


if __name__ == "__main__":
    raise SystemExit(main())
