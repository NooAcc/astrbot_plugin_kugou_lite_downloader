"""渲染用例要用的中文字体。

渲染用例断言「确实画出了图」，就必须有一个含中文字形的字体；而很多 CI / 容器
里一个中文字体都没有，于是整批渲染用例会被 skip——覆盖率静默归零。

这里的策略是：系统已有可用中文字体就用系统字体（更接近线上真实路径）；只有在
系统确实没有时，才现场生成一个**只映射「永」一个字**的最小 TTF 当作回退候选，
让渲染用例照常运行。生成需要 fontTools（见 requirements-dev.txt）；连它也没有，
才 skip，并说明原因。
"""

from __future__ import annotations

import os
import tempfile

import pytest

import search_card

_FILENAME = "test-cjk-min.ttf"
_generated: str | None = None


def write_min_cjk_font(path: str) -> None:
    """写一个只含「永」的最小 TTF。

    字形探测（``search_card._has_cjk_glyph``）要求「永」有非空字形、而私用区码位
    落到空白 ``.notdef``；这里正好构造出这两种情况。
    """
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "uni6C38"])
    builder.setupCharacterMap({0x6C38: "uni6C38"})       # 永
    pen = TTGlyphPen(None)
    pen.moveTo((80, 0))
    pen.lineTo((80, 700))
    pen.lineTo((700, 700))
    pen.lineTo((700, 0))
    pen.closePath()
    builder.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "uni6C38": pen.glyph()})
    builder.setupHorizontalMetrics({".notdef": (500, 0), "uni6C38": (780, 80)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "TestCjkMin", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200,
                     usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.save(path)


def rendering_font_candidates() -> tuple[str, ...]:
    """渲染用例传给 ``extra_font_candidates`` 的候选。

    系统有中文字体时返回空元组（走真实字体）；没有时返回现场生成的最小字体。
    两者都不可用才 skip——此时没有任何字体可供探测，渲染断言无从谈起。
    """
    global _generated
    if search_card.cjk_font_available():
        return ()
    if _generated is None:
        try:
            import fontTools  # noqa: F401
        except ImportError:
            pytest.skip("本机没有中文字体，且未安装 fontTools（见 requirements-dev.txt）")
        directory = tempfile.mkdtemp(prefix="cjk-test-font-")
        path = os.path.join(directory, _FILENAME)
        write_min_cjk_font(path)
        _generated = path
    return (_generated,)
