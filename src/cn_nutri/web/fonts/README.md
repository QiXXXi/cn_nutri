# 部件面板的内置字体

这里的四个文件是**构建产物**，由 [`tools/build_parts_font.py`](../../../tools/build_parts_font.py)
生成，不要手改：

| 文件 | 内容 | 来源 |
|---|---|---|
| `parts-a.ttf` | 155 个字形 | 遍黑体 Plangothic P1 |
| `parts-b.ttf` | 16 个字形 | 遍黑体 Plangothic P2 |
| `parts-c.otf` | 8 个字形 | 思源黑体 Source Han Sans SC |
| `parts.css` | `@font-face` + `unicode-range` | 生成 |

合计 35 KB，覆盖字统网难输入部件表里全部 179 个部件。

## 为什么要三个字体

这 179 个部件里只有 8 个在基本区（`龰龱龴龵龶龷龸龹`，U+9FB0–9FB9），其余 171 个
都在增补平面——这正是字统网当初给它们准备 SVG 骨架线的原因。实测各家字体的覆盖：

| 字体 | 覆盖 |
|---|---|
| 思源黑体 Source Han Sans SC | 19 / 179 ← 扩展 B 一个都没有，**光它不够** |
| 遍黑体 Plangothic P1 | 155 / 179 |
| 遍黑体 Plangothic P2 | 32 / 179 |
| 三个合起来 | 179 / 179 |

两者正好互补：遍黑体本来就是为「补思源黑体没有的字」而做的，而它缺的 8 个基本区
尾字思源黑体有。三个 `@font-face` 共用同一个字体族名 `cn-nutri-parts`，靠
`unicode-range` 分流——浏览器只为页面上真正出现的码位下载对应的那一个文件。

## 许可证

两个源字体都是 **SIL Open Font License 1.1**：

- 思源黑体 © 2014–2025 Adobe，保留字体名 `Source`
- 遍黑体 Plangothic © 2024 Fitzgerald P. Köeingsegg

OFL 允许改写与再分发，但**改了就不能再用保留字体名**，所以这里所有子集都改名成了
`cn-nutri-parts`；版权声明（nameID 0）、许可证（nameID 13/14）、商标（nameID 7）
在子集里原样保留，许可证全文另存为：

- `LICENSE-OFL-SourceHanSans.txt`
- `LICENSE-OFL-Plangothic.txt`

重新生成：

```bash
python -I tools/build_parts_font.py
```

源字体缓存在 `data_raw/fonts/`（约 41 MB，只在重新生成时才需要）。
