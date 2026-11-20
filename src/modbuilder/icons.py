"""MOD のアイテムのアイコン（2026-11-11）。ゲームの中で一目でこの MOD の品と分かるように、「MOD」と書いた 64x64 の画像を作る。

* 補給官で買う消耗品（``itemtransmuterset`` の ``fullBitmap``）: 紫の札・小瓶の絵・「MOD」・「AI SKIN」
* 頭装備（``armor_head`` の ``bitmap``。幻術師でイリュージョンを選ぶ一覧に出る）: 橙の札・人の上半身の絵・「MOD」・「AI SKIN」
* 透明の胴・脚・手・足・肩（``bitmap``）: 半透明の青の札・点線の枠・人の形・「MOD」・「AI SKIN」
* 透明のメダル（``bitmap``。2026-11-11 の利用者の確認の後に追加。D-078）: 公式のメダルのアイコンと同じ 32x32 の、半透明の青の札・
  点線の枠・メダルの形・「MOD」（小さいので「AI SKIN」は入れない）

画像はコードで描く（フォントや画像のファイルを使わない。ドットの字で、アンチエイリアスは無い）。整数の計算だけなので、
どの PC・どの版の Pillow・numpy でもバイト単位で同じになる（決定的なビルドと照合コードのため。公式のアイコンは使わない）。

.tex は、ゲームの公式のアイテムのアイコンと同じ形式で書く: ``TEX`` + 版 2 + u32 0 + u32 長さ + ``DDSR`` + DDS ヘッダー 124 B
（flags 0x1007、ミップ 1 つ、画素形式 DDPF_RGB・32 ビット・マスク 0）+ 非圧縮の画素（上の行から、1 画素 B・G・R・A。アルファは
乗算しない）。2026-11-11 にこの PC のゲームの ``items/misc/bitmaps/invisible_head.tex``・``items/transmutes/head_invisible01a.tex``・
``items/geartorso/bitmaps/a00_torso001.tex`` などで確かめた形（どれも 64x64・ミップ 1 つ・非圧縮の 32 ビット）。
"""
from __future__ import annotations

import os
import struct

import numpy as np

from .naming import ICON_HEAD, ICON_INVISIBLE, ICON_INVISIBLE_MEDAL, ICON_SET

__all__ = ["SIZE", "ICONS", "icon_rgba", "tex_bytes", "icon_files", "icons_needed", "write_icons"]

SIZE = 64

# 大きな字（5x7。3 倍の 15x21 で描く）と小さな字（高さ 5。等倍）。X が字の画素
_BIG = {
    "M": ["X...X", "XX.XX", "X.X.X", "X.X.X", "X...X", "X...X", "X...X"],
    "O": [".XXX.", "X...X", "X...X", "X...X", "X...X", "X...X", ".XXX."],
    "D": ["XXXX.", "X...X", "X...X", "X...X", "X...X", "X...X", "XXXX."],
}
_SMALL = {
    "A": [".XX.", "X..X", "XXXX", "X..X", "X..X"],
    "I": ["XXX", ".X.", ".X.", ".X.", "XXX"],
    "S": [".XXX", "X...", ".XX.", "...X", "XXX."],
    "K": ["X..X", "X.X.", "XX..", "X.X.", "X..X"],
    "N": ["X..X", "XX.X", "X.XX", "X..X", "X..X"],
    " ": ["..", "..", "..", "..", ".."],
}

# 絵（文字ごとの色。. は描かない）
_BOTTLE = (["....cccc....",
            "....cccc....",
            ".....gg.....",
            ".....gg.....",
            "....gggg....",
            "..gggggggg..",
            ".gwlllllllg.",
            ".gwlllllllg.",
            ".gwlllllllg.",
            ".gllllllllg.",
            "..gllllllg..",
            "...gggggg..."],
           {"c": (184, 132, 84), "g": (214, 232, 255), "w": (248, 252, 255), "l": (72, 202, 228)})
_BUST = ([".....hhhh.....",
          "....hhhhhh....",
          "....hhhhhh....",
          "....hhhhhh....",
          ".....hhhh.....",
          "......hh......",
          "...hhhhhhhh...",
          "..hhhhhhhhhh..",
          ".hhhhhhhhhhhh.",
          ".hhhhhhhhhhhh.",
          ".hhhhhhhhhhhh."],
         {"h": (255, 236, 210)})
# メダル（リボンと円盤。透明の部位と同じく、薄い青で塗って明るい水色の縁を付ける）
_MEDAL = ([".x...x.",
           ".xx.xx.",
           "..xxx..",
           "...x...",
           "..xxx..",
           ".xxxxx.",
           ".xxxxx.",
           ".xxxxx.",
           "..xxx.."],
          {"x": (96, 156, 232)})

# 札の色（上と下。縦の段階）と不透明度、枠。size はアイコンの大きさ（既定 64。メダルは公式のメダルのアイコンと同じ 32）
_STYLES = {
    ICON_SET: {"top": (128, 52, 196), "bottom": (58, 12, 104), "alpha": 255, "art": "bottle"},
    ICON_HEAD: {"top": (247, 132, 20), "bottom": (150, 62, 0), "alpha": 255, "art": "bust"},
    ICON_INVISIBLE: {"top": (72, 149, 239), "bottom": (24, 52, 110), "alpha": 176, "art": "ghost", "dashed": True},
    ICON_INVISIBLE_MEDAL: {"top": (72, 149, 239), "bottom": (24, 52, 110), "alpha": 176, "art": "medal", "dashed": True,
                           "size": 32},
}
ICONS = tuple(_STYLES)

_OUTLINE = (12, 12, 16)
_TEXT = (255, 255, 255)
_SUB = (255, 224, 102)
_GHOST = (168, 236, 255)
_GHOST_FILL = (96, 156, 232)
_GHOST_FILL_ALPHA = 216


def _glyphs(text: str, font: dict, gap: int) -> np.ndarray:
    rows = len(font[text[0]])
    width = sum(len(font[c][0]) for c in text) + gap * (len(text) - 1)
    m = np.zeros((rows, width), dtype=bool)
    x = 0
    for c in text:
        g = font[c]
        for y, row in enumerate(g):
            for i, ch in enumerate(row):
                if ch == "X":
                    m[y, x + i] = True
        x += len(g[0]) + gap
    return m


def _scale(m: np.ndarray, k: int) -> np.ndarray:
    return m.repeat(k, axis=0).repeat(k, axis=1)


def _dilate(m: np.ndarray, r: int) -> np.ndarray:
    """正方形（8 近傍）の膨張。外は偽。"""
    h, w = m.shape
    p = np.pad(m, r)
    out = np.zeros_like(m)
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            out |= p[dy:dy + h, dx:dx + w]
    return out


def _erode(m: np.ndarray, r: int) -> np.ndarray:
    """正方形の収縮（外は偽として扱う）。"""
    h, w = m.shape
    p = np.pad(~m, r, constant_values=True)
    hit = np.zeros_like(m)
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            hit |= p[dy:dy + h, dx:dx + w]
    return ~hit


def _rounded(x0: int, y0: int, x1: int, y1: int, r: int, size: int = SIZE) -> np.ndarray:
    """角の丸い長方形（x0..x1・y0..y1 を含む）。判定は画素の中心で、座標を 2 倍にして整数で行う。"""
    yy, xx = np.mgrid[0:size, 0:size]
    px, py = 2 * xx + 1, 2 * yy + 1
    m = (xx >= x0) & (xx <= x1) & (yy >= y0) & (yy <= y1)
    lx, rx = 2 * x0 + 2 * r, 2 * (x1 + 1) - 2 * r
    ty, by = 2 * y0 + 2 * r, 2 * (y1 + 1) - 2 * r
    for cx, cy, inx, iny in ((lx, ty, px < lx, py < ty), (rx, ty, px > rx, py < ty),
                             (lx, by, px < lx, py > by), (rx, by, px > rx, py > by)):
        corner = inx & iny
        m &= ~corner | ((px - cx) ** 2 + (py - cy) ** 2 <= (2 * r) ** 2)
    return m


def _art(rows: list[str], palette: dict) -> tuple[np.ndarray, np.ndarray]:
    h, w = len(rows), max(len(r) for r in rows)
    mask = np.zeros((h, w), dtype=bool)
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != ".":
                mask[y, x] = True
                rgb[y, x] = palette[ch]
    return mask, rgb


def _paste(img: np.ndarray, mask: np.ndarray, color, x0: int, y0: int, alpha: int = 255) -> None:
    """mask の画素を img の (x0, y0) から塗る。color は (r, g, b) か、mask と同じ大きさの (h, w, 3) の配列。"""
    h, w = mask.shape
    region = img[y0:y0 + h, x0:x0 + w]
    if isinstance(color, np.ndarray):
        region[mask, :3] = color[mask]
    else:
        region[mask, :3] = color
    region[mask, 3] = alpha


def icon_rgba(path: str) -> np.ndarray:
    """アイコン（ゲーム内のパス）の画像 → (size, size, 4) の uint8（R, G, B, A。アルファは乗算しない。size は 64、メダルは 32）。"""
    st = _STYLES[path]
    size = st.get("size", SIZE)
    small_icon = size < SIZE                # 32x32（メダル）: 角・枠・点線・字を小さくする
    img = np.zeros((size, size, 4), dtype=np.uint8)
    badge = _rounded(1, 1, size - 2, size - 2, 4 if small_icon else 7, size)
    # 札: 縦の段階（整数の補間）
    top, bottom = np.array(st["top"], dtype=np.int32), np.array(st["bottom"], dtype=np.int32)
    for y in range(size):
        t0, t1 = size - 1 - y, y
        c = (top * t0 + bottom * t1) // (size - 1)
        row = badge[y]
        img[y, row, :3] = c.astype(np.uint8)
        img[y, row, 3] = st["alpha"]
    # 枠（2 画素。32x32 は 1 画素）。透明の部位の札は点線（4 画素ごと。32x32 は 2 画素ごと）
    border = badge & ~_erode(badge, 1 if small_icon else 2)
    if st.get("dashed"):
        yy, xx = np.mgrid[0:size, 0:size]
        border &= ((xx + yy) // (2 if small_icon else 4)) % 2 == 0
        img[border, :3] = _GHOST
        img[border, 3] = 255
    else:
        img[border, :3] = _OUTLINE
        img[border, 3] = 255
    # 絵（上）
    if st["art"] == "bottle":
        m, rgb = _art(*_BOTTLE)
        _paste(img, _dilate(m, 1), _OUTLINE, (size - m.shape[1]) // 2 - 1, 4)
        _paste(img, m, rgb, (size - m.shape[1]) // 2, 5)
    elif st["art"] == "bust":
        m, rgb = _art(*_BUST)
        _paste(img, _dilate(m, 1), _OUTLINE, (size - m.shape[1]) // 2 - 1, 5)
        _paste(img, m, rgb, (size - m.shape[1]) // 2, 6)
    elif st["art"] == "medal":
        # 透明のメダル: メダルの形を、透明の部位と同じく薄い青で塗って明るい水色の縁を付ける
        m, _rgb = _art(*_MEDAL)
        x0 = (size - m.shape[1]) // 2
        _paste(img, _dilate(m, 1), _GHOST, x0 - 1, 2)
        _paste(img, m, _GHOST_FILL, x0, 3, alpha=_GHOST_FILL_ALPHA)
    else:
        # 透明の部位: 人の形を薄い青で塗り、明るい水色の縁を付ける（公式の透明のアイコンと同じ色の考え方）
        m, _rgb = _art(*_BUST)
        x0 = (size - m.shape[1]) // 2
        _paste(img, _dilate(m, 1), _GHOST, x0 - 1, 5)
        _paste(img, m, _GHOST_FILL, x0, 6, alpha=_GHOST_FILL_ALPHA)
    if small_icon:
        # 「MOD」（5x7 の字を等倍。白に 1 画素の黒い縁）。小さいので「AI SKIN」は入れない
        text = _glyphs("MOD", _BIG, 1)
        bx, by = (size - text.shape[1]) // 2, 15
        _paste(img, _dilate(np.pad(text, 1), 1), _OUTLINE, bx - 1, by - 1)
        _paste(img, text, _TEXT, bx, by)
    else:
        # 「MOD」（中央。白に 2 画素の黒い縁）
        big = _scale(_glyphs("MOD", _BIG, 1), 3)
        bx, by = (size - big.shape[1]) // 2, 22
        _paste(img, _dilate(np.pad(big, 2), 2), _OUTLINE, bx - 2, by - 2)
        _paste(img, big, _TEXT, bx, by)
        # 「AI SKIN」（下。黄に 1 画素の黒い縁）
        small = _glyphs("AI SKIN", _SMALL, 1)
        sx, sy = (size - small.shape[1]) // 2, 49
        _paste(img, _dilate(np.pad(small, 1), 1), _OUTLINE, sx - 1, sy - 1)
        _paste(img, small, _SUB, sx, sy)
    # 透明な画素の色は 0 にそろえる（公式のアイコンは 0 でないこともあるが、決まった値にしておく）
    img[img[:, :, 3] == 0, :3] = 0
    return img


def tex_bytes(rgba: np.ndarray) -> bytes:
    """(h, w, 4) の RGBA → 公式のアイテムのアイコンと同じ形式の .tex（非圧縮の 32 ビット、ミップ 1 つ）。"""
    h, w = rgba.shape[:2]
    pixels = np.ascontiguousarray(rgba[:, :, [2, 1, 0, 3]]).tobytes()
    header = (struct.pack("<7I", 124, 0x1007, h, w, 0, 0, 1) + b"\0" * 44
              + struct.pack("<8I", 32, 0x40, 0, 32, 0, 0, 0, 0) + b"\0" * 20)
    body = b"DDSR" + header + pixels
    return b"TEX" + bytes([2]) + struct.pack("<II", 0, len(body)) + body


def icon_files() -> dict[str, bytes]:
    """{ゲーム内のパス: .tex のバイト列}（3 つすべて）。"""
    return {p: tex_bytes(icon_rgba(p)) for p in ICONS}


def icons_needed(has_equipment: bool, has_sets: bool) -> list[str]:
    """ビルドで使うアイコン（参照されるものだけ）。透明の部位（メダルを含む）のレコードは装備が無くても作るので、その分は常に入れる。"""
    out = [ICON_INVISIBLE, ICON_INVISIBLE_MEDAL]
    if has_equipment:
        out.append(ICON_HEAD)
    if has_sets:
        out.append(ICON_SET)
    return out


def write_icons(items_root: str, paths: list[str]) -> list[str]:
    """アイコンを Items の作業フォルダー（``staging/resources/Items``）に書く。ゲーム内のパス ``items/x/y.tex`` は
    ``<items_root>/x/y.tex`` になる（装備のアセットと同じ置き方）。書いたゲーム内のパスの一覧を返す。"""
    files = icon_files()
    for p in paths:
        dst = os.path.join(items_root, *p.split("/")[1:])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(files[p])
    return list(paths)
