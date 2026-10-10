"""难输入部件面板的数据（常用偏旁 / 康熙部首 / 字统网难输入部件）。

分成三组，第三组来自字统网 ``/api/lookup/difficult_components``。

这份数据在构建期被烘成 ``web/data/components.json``：仓库里不带 ``data_raw/``
（194 MB），只带烘好的词库。所以**线上服务读的是那份 JSON**，``data_raw`` 只在
本机重抓时用得上——两处的分组逻辑必须是同一份代码，否则重抓之后面板会变样，
这就有了本模块。
"""

from __future__ import annotations

import itertools
import json
import os

__all__ = ["COMMON_PARTS", "KANGXI", "build_components", "load_components",
           "read_raw", "DEFAULT_RAW", "DEFAULT_JSON"]

#: 常用偏旁（变体形式），第一组
COMMON_PARTS = list("氵亻讠辶艹扌忄犭纟阝灬宀广疒衤礻饣钅车见贝页风飞马鸟鱼"
                    "刂卩廴囗囗彳彡攵攴殳爫爪牜⺮⺊⺈丬丷冫冖凵匚厶尢屮巛弋彐彑")
#: 康熙部首 214 个，第二组
KANGXI = (
    "一丨丶丿乙亅二亠人儿入八冂冖冫几凵刀力勹匕匚匸十卜卩厂厶又口囗土士夂夊夕大女子宀"
    "寸小尢尸屮山巛工己巾干幺广廴廾弋弓彐彡彳心戈戶手支攴文斗斤方无日曰月木欠止歹殳毋"
    "比毛氏气水火爪父爻爿片牙牛犬玄玉瓜瓦甘生用田疋疒癶白皮皿目矛矢石示禸禾穴立竹米糸"
    "缶网羊羽老而耒耳聿肉臣自至臼舌舛舟艮色艸虍虫血行衣襾見角言谷豆豕豸貝赤走足身車辛"
    "辰辵邑酉釆里金長門阜隶隹雨青非面革韋韭音頁風飛食首香馬骨高髟鬥鬯鬲鬼魚鳥鹵鹿麥麻"
    "黃黍黑黹黽鼎鼓鼠鼻齊齒龍龜龠"
)

_HERE = os.path.dirname(os.path.abspath(__file__))
#: 字统网抓下来的原始表。仓库里**没有** data_raw/（194 MB），只有本机重抓时才在
DEFAULT_RAW = os.path.normpath(os.path.join(
    _HERE, "..", "..", "data_raw", "zitools", "_difficult_components.json"))
#: 构建期烘出来的成品，这份才随仓库走
DEFAULT_JSON = os.path.join(_HERE, "web", "data", "components.json")


def _stroke_key(key: str) -> int:
    """分组键是 ``"1"``…``"12"``、``"13+"``。按字符串排会把 10 排到 2 前面。"""
    digits = "".join(itertools.takewhile(str.isdigit, key))
    return int(digits) if digits else 999


def _dedup(seq) -> list[str]:
    seen, out = set(), []
    for c in seq:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def read_raw(path: str | None = None) -> dict | None:
    """读字统网抓下来的原始表；没有就返回 None。"""
    try:
        with open(path or DEFAULT_RAW, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def build_components(raw: dict | None) -> dict:
    """``{"groups": [{"label", "chars"}, …]}``，按笔画数升序。

    字形不随数据下发——179 个部件里有 171 个在增补平面，前端用内置的
    ``web/fonts/`` 子集字体直接当文字渲染（见 tools/build_parts_font.py）。
    """
    groups = [{"label": "常用偏旁", "chars": _dedup(COMMON_PARTS)},
              {"label": "康熙部首", "chars": _dedup(KANGXI)}]
    for key, cols in sorted((raw or {}).get("res", {}).items(),
                            key=lambda kv: _stroke_key(kv[0])):
        chars = _dedup([c for col in cols for c in col])
        if chars:
            label = f"{key.rstrip('+')} 画以上" if key.endswith("+") else f"{key} 画"
            groups.append({"label": label, "chars": chars})
    return {"groups": groups}


def load_components(json_path: str | None = None,
                    raw_path: str | None = None) -> dict:
    """优先读烘好的 JSON（仓库/线上都是这条路），没有才回头从 data_raw 现建。"""
    try:
        with open(json_path or DEFAULT_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("groups"):
            return data
    except (OSError, ValueError):
        pass
    return build_components(read_raw(raw_path))
