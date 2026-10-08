"""Shared matplotlib chart helpers (Phase 16)."""

import base64

import numpy as np

from guban.charts import COLORS, figure, img_tag, money, save_png, to_data_uri

PNG = b"\x89PNG\r\n\x1a\n"


def test_data_uri_is_an_inline_png():
    fig, ax = figure()
    ax.plot(np.arange(10), np.arange(10) ** 2, color=COLORS["s1"])
    uri = to_data_uri(fig)
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1])[:8] == PNG


def test_save_png_writes_a_file(tmp_path):
    fig, ax = figure(rows=1, cols=2)
    ax[0].bar([1, 2], [3, 4])
    path = save_png(fig, tmp_path / "sub" / "x.png")
    assert path.read_bytes()[:8] == PNG


def test_img_tag_escapes_alt_text():
    tag = img_tag("data:image/png;base64,AAAA", 'a <b> & "c"', cls="chart short")
    assert tag.startswith("<img class='chart short' ") and "&lt;b&gt; &amp; &quot;c&quot;" in tag and "alt=" in tag


def test_money_axis_format():
    assert money(-1234.5) == "−1,234" and money(1234.5) == "1,234" and money(0) == "0"
