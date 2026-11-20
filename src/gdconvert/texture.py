"""TextureProcessor: ベースカラーの取り出し・焼き直し・2 の累乗化・余白の塗り足し・.tex 化と検証。

.tex は ゲーム付属の ``TextureCompiler.exe`` で作る（形式は非公開ではないが、公式ツールの出力をそのまま使う）。
2026-10-16 の確認事項（prototype/mod02）:

* ``-format`` は小文字の ``dxt1`` / ``dxt3`` / ``dxt5`` だけが効く。大文字や未知の値はエラーにならず無圧縮 24bit になる
* .tex = ``TEX`` + u8 版(2) + u32 0 + u32 長さ + ``DDSR`` + DDS ヘッダー 124 B + ミップ（**小さい順**）
"""
from __future__ import annotations

import locale
import os
import secrets
import shutil
import struct
import subprocess

import numpy as np
from scipy import ndimage

from . import geometry
from ._i18n import tr
from .gltf import Model

__all__ = [
    "pow2_floor", "choose_size", "resize", "pad_charts", "coverage_mask", "sample_bilinear",
    "bake_from_model", "single_texture", "needs_bake", "write_png", "compile_tex", "read_tex",
    "decode_top_mip", "verify_tex",
]


# .tex の各辺の最小（DXT は 4x4 画素のブロック。TextureCompiler は 1x1 の画像をミップ数 2 で書き、modbuilder の
# assets.check_tex が拒む。R04A-01）
MIN_SIDE = 4


def pow2_floor(x: int) -> int:
    return 1 << max(0, int(x).bit_length() - 1)


def choose_size(w: int, h: int, max_size: int, min_size: int) -> tuple[int, int]:
    """各辺を 2 の累乗に。元が 2 の累乗ならそのまま、そうでなければ近い方（上限 ``max_size``）。

    上限・下限が 2 の累乗でないときは、上限はそれ以下の、下限はそれ以上の 2 の累乗にそろえる（下限は上限を超えない）。
    そのまま切ると 2 の累乗でない大きさになり、TextureCompiler が工程 4 で拒否する（2026-10-20 点検 R04A-03）。

    各辺は :data:`MIN_SIDE`（4）以上（2026-11-04 の利用者の決定、R04A-01）。上限・下限が 4 未満でも 4 にする
    （``ConvertConfig.validate`` が 4 未満を断るので、検査を通らない値が直接渡されたときの多重の守り）。上限・下限が
    4 以上なら結果は前と同じ。"""
    hi = max(pow2_floor(max_size), MIN_SIDE)
    lo_size = min(max(1 << max(0, int(min_size) - 1).bit_length(), MIN_SIDE), hi)
    def one(x):
        if x & (x - 1) == 0:
            p = x
        else:
            lo = pow2_floor(x)
            p = lo * 2 if (x - lo) > (lo * 2 - x) else lo
        return int(min(max(p, lo_size), hi))
    return one(w), one(h)


def resize(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """LANCZOS で (h, w) にする。入力と同じチャンネル数を返す。

    RGB とアルファは別々に補間する（Pillow は RGBA を事前乗算して補間するので、アルファ 0 の画素の色が黒になる。
    glTF の OPAQUE ではアルファは無視すべき。アルファが全て 255 の入力では、まとめて補間した結果とビット単位で同じ）。
    """
    from PIL import Image
    if img.shape[1] == w and img.shape[0] == h:
        return img
    rgb = np.asarray(Image.fromarray(np.ascontiguousarray(img[..., :3]), "RGB").resize((w, h), Image.LANCZOS),
                     dtype=np.uint8)
    if img.shape[2] == 3:
        return rgb
    a = np.asarray(Image.fromarray(np.ascontiguousarray(img[..., 3]), "L").resize((w, h), Image.LANCZOS),
                   dtype=np.uint8)
    return np.dstack([rgb, a])


def coverage_mask(uvs: np.ndarray, faces: np.ndarray, w: int, h: int, dilate: int = 1) -> np.ndarray:
    """UV 三角形が覆う画素（境界の取りこぼしを防ぐため ``dilate`` 画素広げる）。"""
    m = geometry.rasterize_uv_coverage(uvs, faces, w, h)
    if dilate > 0:
        m = ndimage.binary_dilation(m, iterations=dilate)
    return m


def pad_charts(img: np.ndarray, mask: np.ndarray, max_px: int | None = None) -> np.ndarray:
    """覆われていない画素を、最も近い覆われた画素の色で塗る（ミップと DXT 圧縮で継ぎ目に背景色がにじまないように）。"""
    if mask.all() or not mask.any():
        return img
    dist, (iy, ix) = ndimage.distance_transform_edt(~mask, return_indices=True)
    out = img[iy, ix]
    if max_px is not None:
        keep = dist > max_px
        out[keep] = img[keep]
    return out


def sample_bilinear(img: np.ndarray, uv: np.ndarray, wrap: bool = True) -> np.ndarray:
    """UV（v 0 = 上端）で双一次補間。戻り値 float32 (n, c)、0〜255。"""
    h, w = img.shape[:2]
    x = uv[:, 0] * w - 0.5
    y = uv[:, 1] * h - 0.5
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = (x - x0)[:, None]
    fy = (y - y0)[:, None]

    def idx(a, n):
        return np.mod(a, n) if wrap else np.clip(a, 0, n - 1)
    xa, xb = idx(x0, w), idx(x0 + 1, w)
    ya, yb = idx(y0, h), idx(y0 + 1, h)
    im = img.astype(np.float32)
    c = (im[ya, xa] * (1 - fx) * (1 - fy) + im[ya, xb] * fx * (1 - fy)
         + im[yb, xa] * (1 - fx) * fy + im[yb, xb] * fx * fy)
    return c


def _used_material(model: Model):
    used = np.unique(model.face_material)
    return model.materials[int(used[0])] if len(used) == 1 else None


def _compose_linear(texel: np.ndarray | None, factor, vc: np.ndarray | None, n: int) -> np.ndarray:
    """ベースカラーの合成（glTF 仕様: テクスチャの RGB は sRGB、係数と頂点色は線形）。戻り値 (n, 4) float、0〜255。

    texel: (n, 4) の sRGB の 8 bit 値（float）か None（テクスチャ無し = 1）。vc: (n, 4) の線形の頂点色か None。
    RGB は線形に戻して掛けてから sRGB に戻す。アルファは線形のまま掛ける。
    """
    from .gltf import linear_to_srgb, srgb_to_linear
    f = np.asarray(factor, dtype=np.float64)
    if texel is None:
        rgb = np.ones((n, 3))
        a = np.ones(n)
    else:
        rgb = srgb_to_linear(np.asarray(texel[:, :3], dtype=np.float64) / 255.0)
        a = np.asarray(texel[:, 3], dtype=np.float64) / 255.0
    rgb = rgb * f[None, :3]
    a = a * f[3]
    if vc is not None:
        rgb = rgb * vc[:, :3]
        a = a * vc[:, 3]
    return np.concatenate([linear_to_srgb(rgb) * 255.0, (np.clip(a, 0, 1) * 255.0)[:, None]], axis=1)


def _passthrough(factor, colors: np.ndarray | None) -> bool:
    """係数の RGB が 1 で頂点色（の RGB）も無い・1 なら、色の変換をせず今までどおりの式で通す
    （TRELLIS の出力のバイト列と、ゲーム内で確認済みの結果を変えない）。"""
    return bool(np.allclose(np.asarray(factor)[:3], 1.0)
                and (colors is None or np.allclose(np.asarray(colors)[:, :3], 1.0)))


# single_texture で係数を合成する 1 回分の画素数（一時配列は約 50 MB）
_COMPOSE_BLOCK_PX = 1 << 18


def single_texture(model: Model) -> np.ndarray | None:
    """焼き直し不要なら、その 1 枚の RGBA を返す（係数を線形で掛けたもの）。"""
    if needs_bake(model):
        return None
    mat = _used_material(model)
    img = mat.image
    f = np.array(mat.base_color_factor, dtype=np.float32)
    if np.allclose(f, 1.0):
        return img
    alpha_only = _passthrough(f, None)          # アルファの係数だけ（今までと同じ式）
    # 行のまとまりごとに合成する（float64 の一時配列を全画素ぶん作ると 8192² で約 10 GB になる。2026-10-20 点検
    # R04B-04。アルファの係数だけのときも float32 の一時配列で画像の 8 倍になるので同じにする。2026-10-27 点検
    # R04B-02。計算は画素ごとなので、結果は全画素を一度に合成したものとバイト単位で同じ）
    h, w = img.shape[:2]
    out = np.empty((h, w, 4), dtype=np.uint8)
    step = max(1, _COMPOSE_BLOCK_PX // w)
    for y in range(0, h, step):
        if alpha_only:
            out[y:y + step] = np.clip(img[y:y + step].astype(np.float32) * f[None, None, :], 0, 255).astype(np.uint8)
            continue
        blk = img[y:y + step].reshape(-1, 4).astype(np.float64)
        c = _compose_linear(blk, f, None, len(blk))
        out[y:y + step] = np.clip(np.round(c), 0, 255).astype(np.uint8).reshape(-1, w, 4)
    return out


def cutout_mask(model: Model, img: np.ndarray) -> np.ndarray | None:
    """1 枚のテクスチャのマテリアルが MASK のとき、切り抜かれる画素（アルファ < alphaCutoff）。それ以外は None。

    ``img`` は :func:`single_texture` の結果（係数を掛けたもの）を縮小したもの。切り抜かれる画素は覆われていない
    画素として扱い、:func:`pad_charts` で近くの不透明な色で塗る（ゲームでは不透明の面として出るので、黒などの
    透明画素の色を出さない）。"""
    mat = _used_material(model)
    if mat is None or mat.alpha_mode != "MASK" or img.shape[2] < 4:
        return None
    return img[..., 3].astype(np.float64) < mat.alpha_cutoff * 255.0


def needs_bake(model: Model) -> bool:
    """1 枚のテクスチャと [0,1] の UV で表せない入力（複数マテリアル、テクスチャ無し、頂点色、UV の繰り返し）。"""
    used = np.unique(model.face_material)
    if len(used) != 1:
        return True
    mat = model.materials[int(used[0])]
    if not mat.has_texture or model.uvs is None:
        return True
    if model.colors is not None and not np.allclose(model.colors, 1.0):
        return True
    uv = model.uvs[np.unique(model.faces)]
    if not np.isfinite(uv).all():       # NaN は比べると常に偽なので先に見る（2026-10-27 点検 R04B-01）
        return True
    return bool((uv.min() < -1e-3) or (uv.max() > 1 + 1e-3))


def bake_from_model(src: Model, dst_positions: np.ndarray, dst_faces: np.ndarray, dst_uvs: np.ndarray,
                    width: int, height: int, padding: int = 8, log=None) -> np.ndarray:
    """``dst``（新しい UV を持つメッシュ）のテクスチャを、``src`` の表面色から焼く。

    各画素の 3D 位置から src の表面の最近点を求め、そのマテリアルの色（テクスチャ × 係数 × 頂点色）を取る。
    合成は線形で行う（テクスチャの標本化自体は今までどおり sRGB の 8 bit 値の双一次補間。係数の RGB が 1 で
    頂点色も無いマテリアルは変換せず今までの式で通す）。MASK のマテリアルでアルファが alphaCutoff 未満の画素は
    覆われていない画素として扱い、近くの色で塗る。法線が逆向きの面には距離の罰則を付ける（薄い板の裏の色を
    拾わないため）。UV・頂点色が数値でない（NaN・inf）面から取る画素も覆われていない画素として扱う（色が不定に
    なるため。2026-10-27 点検 R04B-01）。戻り値 (h, w, 4) uint8。
    """
    fid, bary = geometry.rasterize_uv(dst_uvs, dst_faces, width, height)
    ys, xs = np.nonzero(fid >= 0)
    f = fid[ys, xs]
    b = bary[ys, xs]
    tri = np.asarray(dst_faces)[f]
    P = np.asarray(dst_positions, dtype=np.float64)
    pts = b[:, 0:1] * P[tri[:, 0]] + b[:, 1:2] * P[tri[:, 1]] + b[:, 2:3] * P[tri[:, 2]]
    dn = geometry.face_normals(P, dst_faces)[f]
    q = geometry.SurfaceQuery(src.positions, src.faces)
    diag = float(np.linalg.norm(src.positions.max(0) - src.positions.min(0)))
    sf, sb, _, _ = q.query(pts, k=12, normals=dn, normal_penalty=0.05 * diag)
    if log:
        log(tr(f"  焼き込み: {len(pts)} 画素", f"  Baking: {len(pts)} pixels"))
    color = np.zeros((len(pts), 4), dtype=np.float32)
    cut = np.zeros(len(pts), dtype=bool)
    bad = np.zeros(len(pts), dtype=bool)
    stri = src.faces[sf]
    for mi, mat in enumerate(src.materials):
        sel = np.flatnonzero(src.face_material[sf] == mi)
        if len(sel) == 0:
            continue
        texel = vc = None
        if mat.has_texture and src.uvs is not None:
            uv = (sb[sel, 0:1] * src.uvs[stri[sel, 0]] + sb[sel, 1:2] * src.uvs[stri[sel, 1]]
                  + sb[sel, 2:3] * src.uvs[stri[sel, 2]])
            nf = ~np.isfinite(uv).all(axis=1)
            if nf.any():
                bad[sel[nf]] = True
                uv = np.where(nf[:, None], 0.0, uv)
            texel = sample_bilinear(mat.image, uv)
        if src.colors is not None:
            vc = (sb[sel, 0:1] * src.colors[stri[sel, 0]] + sb[sel, 1:2] * src.colors[stri[sel, 1]]
                  + sb[sel, 2:3] * src.colors[stri[sel, 2]])
        if _passthrough(mat.base_color_factor, src.colors):     # 今までと同じ式（バイト列を変えない）
            fac = np.array(mat.base_color_factor, dtype=np.float32) * 255.0
            c = texel * (fac / 255.0) if texel is not None else np.repeat(fac[None, :], len(sel), axis=0)
            if vc is not None:
                c = c * vc
        else:
            c = _compose_linear(texel, mat.base_color_factor, vc, len(sel))
        color[sel] = c
        if mat.alpha_mode == "MASK":
            cut[sel] = c[:, 3] < mat.alpha_cutoff * 255.0
    bad |= ~np.isfinite(color).all(axis=1)
    if bad.any():
        color[bad] = 0.0
        cut |= bad
    img = np.zeros((height, width, 4), dtype=np.uint8)
    img[ys, xs] = np.clip(np.round(color), 0, 255).astype(np.uint8)
    mask = np.zeros((height, width), dtype=bool)
    mask[ys, xs] = True
    if cut.any():
        mask[ys[cut], xs[cut]] = False
    return pad_charts(img, mask)


# ----------------------------------------------------------------------
# 書き出しと .tex
# ----------------------------------------------------------------------
def write_png(path: str, img: np.ndarray, rgb_only: bool = True) -> None:
    from PIL import Image
    a = np.ascontiguousarray(img[..., :3] if rgb_only else img)
    Image.fromarray(a, "RGB" if a.shape[2] == 3 else "RGBA").save(path, format="PNG")


# TextureCompiler の打ち切り（2048 px で 3 秒弱。止まったときは無期限に待つので、余裕を持たせて検出だけする）
TOOL_TIMEOUT = 300.0
# ツールの出力の読み方（ANSI コードページ。作業プロセスは -X utf8 で動くので、指定しないと日本語のパスを含む
# エラーの文が UnicodeDecodeError で失われる）
_ANSI = "mbcs" if os.name == "nt" else locale.getencoding()


def _plain_name(name: str) -> bool:
    """ツールにそのまま渡せるファイル名（ASCII の印字可能文字だけ）。"""
    return name.isascii() and name.isprintable()


def _ansi_note(path: str) -> str:
    try:
        path.encode(_ANSI, "strict")
        return ""
    except UnicodeEncodeError:
        return tr("（パスに Windows の文字コード（ANSI）で表せない文字があります。ASCII だけのフォルダーで試してください）",
                  " (the path has characters that the Windows code page (ANSI) cannot represent; try a folder whose "
                  "path is ASCII only)")


def compile_tex(gd: str, src_png: str, dst_tex: str, fmt: str = "dxt1", timeout: float = TOOL_TIMEOUT) -> dict:
    """TextureCompiler.exe で .tex にし、fourcc を確かめる。

    ツールはパスを ANSI（A 版の API）で扱い、ANSI コードページに無い字（é・한 など）を含むパスを開けない。そこで
    出力先のフォルダーを作業フォルダー（cwd）にして ASCII のファイル名だけを渡す。入力が別のフォルダーにあるか、
    ファイル名が ASCII でないときは、出力先のフォルダーに一意の ASCII の一時名で写してから呼び、``os.replace`` で
    ``dst_tex`` にする（出力はパスの渡し方によらずバイト一致。2026-10-17 点検 C16）。
    """
    fmt = fmt.lower()
    if fmt not in ("dxt1", "dxt3", "dxt5"):
        raise ValueError(tr(f"未対応の形式: {fmt}", f"Unsupported format: {fmt}"))
    tool = os.path.join(gd, "TextureCompiler.exe")
    if not os.path.isfile(tool):
        raise FileNotFoundError(tr(f"TextureCompiler.exe が見つからない: {tool}", f"TextureCompiler.exe was not found: {tool}"))
    src_png = os.path.abspath(src_png)
    dst_tex = os.path.abspath(dst_tex)
    work = os.path.dirname(dst_tex)
    if os.path.isfile(dst_tex):
        os.remove(dst_tex)
    tag = f"_tc_{os.getpid()}_{secrets.token_hex(4)}"     # 同じフォルダーでの同時実行でもぶつからない名前
    temps = []
    if os.path.normcase(os.path.dirname(src_png)) == os.path.normcase(work) and _plain_name(os.path.basename(src_png)):
        src_arg = os.path.basename(src_png)
    else:
        src_arg = tag + ".png"
        temps.append(os.path.join(work, src_arg))
        shutil.copyfile(src_png, temps[-1])
    out_arg = os.path.basename(dst_tex) if _plain_name(os.path.basename(dst_tex)) else tag + ".tex"
    out_full = os.path.join(work, out_arg)
    note = _ansi_note(work)
    ok = False
    try:
        try:
            r = subprocess.run([tool, "-format", fmt, src_arg, out_arg], cwd=work, capture_output=True, text=True,
                               encoding=_ANSI, errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(tr(f"ゲーム付属のツール（TextureCompiler）が {timeout:.0f} 秒応答しません: {src_png}{note}",
                                  f"The game's bundled tool (TextureCompiler) did not respond for {timeout:.0f} seconds: "
                                  f"{src_png}{note}")) from None
        if r.returncode != 0 or not os.path.isfile(out_full):
            raise RuntimeError(tr(f"TextureCompiler 失敗: {src_png}{note}\n{r.stdout}{r.stderr}",
                                  f"TextureCompiler failed: {src_png}{note}\n{r.stdout}{r.stderr}"))
        if out_full != dst_tex:
            os.replace(out_full, dst_tex)
        ok = True
    finally:
        # 写した入力と、失敗・打ち切りで残った途中の出力は消す
        for t in temps + ([] if ok else [out_full]):
            if os.path.isfile(t):
                os.remove(t)
    info = read_tex(dst_tex)
    if info["fourcc"] != fmt.upper().encode():
        raise RuntimeError(tr(f"{fmt.upper()} になっていない: {dst_tex}（{info['fourcc']!r}）",
                              f"The output is not {fmt.upper()}: {dst_tex} ({info['fourcc']!r})"))
    return info


def read_tex(path: str) -> dict:
    with open(path, "rb") as f:
        d = f.read()
    if d[:3] != b"TEX":
        raise ValueError(tr(f"{path}: TEX ヘッダーではない", f"{path}: not a TEX header"))
    _zero, size = struct.unpack_from("<II", d, 4)
    dds = d[12:]
    if dds[:4] != b"DDSR" or size != len(dds):
        raise ValueError(tr(f"{path}: DDSR ヘッダー/長さが不正", f"{path}: invalid DDSR header or length"))
    h, w = struct.unpack_from("<II", dds, 12)
    mips = struct.unpack_from("<I", dds, 28)[0]
    return {"version": d[3], "width": w, "height": h, "mips": mips, "fourcc": dds[84:88], "data": dds[128:],
            "size": len(d)}


def _dxt_mip_sizes(w, h, mips, block):
    return [max(1, (max(1, w >> i) + 3) // 4) * max(1, (max(1, h >> i) + 3) // 4) * block for i in range(mips)]


def decode_top_mip(info: dict) -> np.ndarray:
    """DXT1 の最大ミップ（データ末尾）を RGB に復号する（検証用）。"""
    if info["fourcc"] != b"DXT1":
        raise ValueError(tr("DXT1 以外の復号は未対応", "Only DXT1 can be decoded"))
    w, h = info["width"], info["height"]
    sizes = _dxt_mip_sizes(w, h, info["mips"], 8)
    if sum(sizes) != len(info["data"]):
        raise ValueError(tr("ミップサイズの合計がデータ長と一致しない", "The total of the mip sizes does not match the data length"))
    top = info["data"][-sizes[0]:]
    blocks = np.frombuffer(top, dtype="<u2").reshape(-1, 4)
    c0 = blocks[:, 0].astype(np.int32)
    c1 = blocks[:, 1].astype(np.int32)
    bits = blocks[:, 2].astype(np.uint32) | (blocks[:, 3].astype(np.uint32) << 16)

    def rgb(c):
        return np.stack([((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31], axis=1)
    a, b = rgb(c0), rgb(c1)
    four = (c0 > c1)[:, None]
    p2 = np.where(four, (2 * a + b) // 3, (a + b) // 2)
    p3 = np.where(four, (a + 2 * b) // 3, 0)
    pal = np.stack([a, b, p2, p3], axis=1)
    idx = (bits[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3
    px = np.take_along_axis(pal, idx[:, :, None].astype(np.int64).repeat(3, axis=2), axis=1)
    bw, bh = max(1, w // 4), max(1, h // 4)
    return px.reshape(bh, bw, 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 3)[:h, :w].astype(np.uint8)


def verify_tex(tex_path: str, src_rgb: np.ndarray) -> dict:
    """.tex を復号して元画像と比べる（DXT1 は非可逆なので平均誤差。上下反転していないことも確認）。

    上下の向きは、元画像が上下で区別できるときだけ確かめる（一様な色・上下対称の画像では反転しても誤差が同じなので
    確かめられない）。元画像の上下の自己差 d_sym = mean|src - src[::-1]| が、向きに依存しない圧縮誤差
    min(err, err_flip) の 2 倍（かつ 1.0）以上のときに ``err_flip > err`` を条件に加える（``orientation_checked``）。

    誤差は、画素ごとの平均誤差 err が 6.0 未満か、4x4 ブロックの平均色どうしの平均誤差 block_err が 3.5 未満なら合格
    にする。DXT1 はブロック内の色を 2 色の補間に減らすので、細かい模様・粒状のノイズでは正常な出力でも err が 6 を
    超える（綾織りの模様で 12.4）が、ブロックの平均色はほぼ保つ（正常な出力で 0.5〜2.9、ずれ・チャンネルの入れ替え・
    反転などの壊れた出力は 4.8 以上。2026-10-20 点検 R04B-01）。block_err で合格にしたときは ``warning`` を残す。

    1 ブロックに離れた 3〜4 色が混ざる多色の細かい模様（1 画素ごとの迷彩・スパンコール状の柄）では、正常な出力でも
    block_err が 4.7〜12 になる。そこで 3 つ目の経路として、復号結果が「自分を壊した形」（上下・左右の反転、
    ±1・±2・±4 画素のずらし、RGB の並べ替え、灰色化）のどれよりも明らかに元画像に近く（err がそれらの最小の
    0.9 倍未満。本物の TextureCompiler の正常な出力で 0.59〜0.86、ずれ・反転・入れ替え・灰色化は 1.0 以上）、
    画像全体の平均色の差（``mean_color_error``）が 12.0 未満（正常な出力で 0.2〜8.1、明るさが半分の出力は
    19 以上）なら、``warning`` 付きで合格にする（2026-10-27 点検 R04B-03）。

    辺が 4 画素未満の画像は 1 列のブロックに収まり、一直線上にない 3 色以上を DXT1 で表せないので、正常な出力でも
    画素の誤差がいくらでも大きくなる（2x2 の 4 色で 42.5）。色の誤差では判定できないので、大きさ・形式・データ長
    （:func:`decode_top_mip` で確認）だけで合格にし、err が 6.0 以上なら ``warning`` を残す（2026-10-27 点検 R04A-01）。
    """
    info = read_tex(tex_path)
    dec = decode_top_mip(info).astype(np.int32)
    src = src_rgb[..., :3].astype(np.int32)
    same_size = info["width"] == src.shape[1] and info["height"] == src.shape[0]
    if not same_size:
        return {"ok": False, "width": info["width"], "height": info["height"], "mips": info["mips"],
                "fourcc": info["fourcc"].decode("ascii", "replace"), "bytes": info["size"],
                "source_size": [int(src.shape[1]), int(src.shape[0])]}
    err = float(np.abs(dec - src).mean())
    err_flip = float(np.abs(dec[::-1] - src).mean())
    d_sym = float(np.abs(src - src[::-1]).mean())
    orientation_checked = d_sym >= max(1.0, 2.0 * min(err, err_flip))
    tiny = min(src.shape[:2]) < 4
    block_err = float(np.abs(_block_mean(dec) - _block_mean(src)).mean()) if not tiny else err
    warning = None
    if tiny:
        ok = True
        orientation_checked = False
        if err >= 6.0:
            warning = tr("辺が 4 画素未満のため DXT1 の圧縮誤差は確かめていない（大きさと形式だけ確認）",
                         "The DXT1 compression error was not checked because a side is shorter than 4 pixels "
                         "(only the size and the format were checked)")
    else:
        ok = (err < 6.0 or block_err < 3.5) and (err_flip > err or not orientation_checked)
        if ok and err >= 6.0:
            warning = tr("細かい模様のため DXT1 の圧縮誤差が大きい（4x4 ブロックの平均色は一致）",
                         "Large DXT1 compression error because of fine patterns (the 4x4 block averages match)")
    rel = mean_err = None
    if not ok and (err_flip > err or not orientation_checked):
        mean_err = float(np.abs(dec.reshape(-1, 3).mean(0) - src.reshape(-1, 3).mean(0)).mean())
        alt = _min_broken_error(dec, src)
        if alt is not None and alt > 0:
            rel = err / alt
            if rel < 0.9 and mean_err < 12.0:
                ok = True
                warning = tr("多色の細かい模様のため DXT1 の圧縮誤差が大きい（ずれ・反転・チャンネルの入れ替えは無い）",
                             "Large DXT1 compression error because of fine multicolored patterns (no shift, flip "
                             "or channel swap was found)")
    res = {"ok": bool(ok), "mean_abs_error": round(err, 3), "mean_abs_error_if_flipped": round(err_flip, 3),
           "block_mean_abs_error": round(block_err, 3),
           "d_sym": round(d_sym, 3), "orientation_checked": bool(orientation_checked),
           "width": info["width"], "height": info["height"], "mips": info["mips"],
           "fourcc": info["fourcc"].decode("ascii", "replace"), "bytes": info["size"]}
    if rel is not None:
        res["relative_error"] = round(rel, 3)
        res["mean_color_error"] = round(mean_err, 3)
    if warning:
        res["warning"] = warning
    return res


def _min_broken_error(dec: np.ndarray, src: np.ndarray) -> float | None:
    """復号結果を「壊した形」（反転・ずらし・RGB の並べ替え・灰色化）にしたものと元画像の平均誤差の最小値。
    dec と同じになる形（周期の合う柄のずらし・灰色の画像の並べ替えなど）は除く。候補が無ければ None。"""
    def shapes():
        yield dec[::-1]
        yield dec[:, ::-1]
        for s in (1, 2, 4):
            for sg in (1, -1):
                yield np.roll(dec, sg * s, axis=1)
                yield np.roll(dec, sg * s, axis=0)
        for p in ((0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)):
            yield dec[..., list(p)]
        yield np.repeat(dec.mean(-1, keepdims=True).astype(np.int32), 3, axis=-1)
    best = None
    for v in shapes():          # 1 つずつ作って比べる（一時配列を画像の数枚分に抑える）
        if np.array_equal(v, dec):
            continue
        e = float(np.abs(v - src).mean())
        best = e if best is None else min(best, e)
    return best


def _block_mean(a: np.ndarray, n: int = 4) -> np.ndarray:
    """(h, w, c) を n x n のブロックの平均にする（端の n に満たない行・列は除く）。"""
    h, w = a.shape[0] // n * n, a.shape[1] // n * n
    return a[:h, :w].reshape(h // n, n, w // n, n, -1).mean(axis=(1, 3))
