"""生成ジョブ: 入力（画像・テキスト・GLB）→ 3D 生成（生成 AI の子プロセス）→ 変換（作業プロセス）→ 下書き。

進捗の割り当て: 生成 0〜0.70、変換 0.70〜1.0（GLB の入力は変換だけ 0.05〜1.0）。
失敗したときは下書きに記録して残し（画面で原因を確認できるように）、取消のときは下書きを消す。
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import os
import shutil
import struct
import traceback
import warnings as _warnings

from . import envcheck, providers, redact
from .errors import BackendError, JobCancelled, failure_text
from .i18n import tr
from .providers.base import GenerationRequest
from .settings import BG_OPTION_KEYS, resolve_game_dir, resolve_trellis_root, validate_generation

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
# 中身で受け付ける形式（Pillow の形式名。IMAGE_EXTS と同じ 4 形式。拡張子と中身の食い違いはこの中だけ許す）。指定しないと
# 拡張子 .png の EPS などでも開き、EPS は Ghostscript（PATH にあれば）を起動してしまう（点検 R10B-04）。
# カメラの JPEG（MPO）は JPEG として開ける
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "BMP")
MAX_IMAGES = 4
LABEL_MAX = 32
MAX_IMAGE_BYTES = 40 * 1024 * 1024
# 受け付ける画像の大きさ（点検 C49・C51）。一辺と画素数の両方で決める。画素数の上限は Pillow の DecompressionBombError
# （既定で約 1.79 億画素）より下にして、受け付ける範囲では Pillow が止めないようにする（警告は _open_image で抑える）。
# TRELLIS はどのみち 1,024 px 以下（透明な画像は 518 px）に縮めるので、生成に渡す画像は WORK_IMAGE_SIDE まで縮める
MAX_IMAGE_SIDE = 16384
MAX_IMAGE_PIXELS = 160_000_000
WORK_IMAGE_SIDE = 4096
MIN_IMAGE_SIDE = 64
# TRELLIS（preprocess_image）は不透明度 0.8 を超える範囲（alpha 205 以上）で切り抜き、518×518 にする
ALPHA_OPAQUE = 205
TRELLIS_IMAGE_SIZE = 518
# 透明部分はあるのに、ほぼ全体が不透明（角だけ透明なスクリーンショットなど）。TRELLIS は alpha が 1 画素でも 255 未満なら
# 背景を除かないので、背景ごと 3D になる。止めずに注意だけを出す（点検 C09。画面の ImageSlot も同じ閾値）
NEARLY_OPAQUE_COVERAGE = 0.95
_GRAY16_MODES = ("I;16", "I;16L", "I;16B", "I;16N", "I")
MAX_TEXT = 1000
GEN_RANGE = (0.0, 0.70)
CONV_RANGE = (0.70, 1.0)
# GPU メモリの閾値は envcheck.VRAM_MIN_MIB・VRAM_REC_MIB（名目の 12 GB・16 GB より 512 MiB 下。点検 C03）。
# 生成の計測の最大はデバイス全体で 14〜15 GB（docs/trellis_setup.md 8.3、trellis_text_and_samples.md 3 章）
VRAM_FREE_WARN_MIB = 14 * 1024
# 空きの警告は「総量 − この値」も上限にする。総量が 14 GB 未満の GPU（12 GB 級）で、他のアプリを閉じても消えない
# 警告を毎回出さないため（点検 FG02-R2。12 GB 級の注意は gpu_memory の warning の文で別に出す）
VRAM_FREE_SLACK_MIB = 1536
# 変換の前の y 軸回りの回転（度）。生成 AI の出力の正面が −Z を向いたとき、利用者が 180 を選んで変換し直す（docs/app_architecture.md 12 章 #16）
YAW_CHOICES = (0, 180)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 入力の検査
# ---------------------------------------------------------------------------
def utf8_ok(s: str) -> bool:
    """UTF-8 にできる文字列か（JSON の "\\ud800" のような対になっていないサロゲートを含むと、下書き・ログ・指紋の
    書き込みで UnicodeEncodeError になる。点検 R10A-04・R10B-03）。"""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def text_invalid_chars() -> BackendError:
    return BackendError("INVALID_INPUT", tr("文章に使えない文字（壊れた文字。対になっていないサロゲート）があります",
                                            "The text contains characters that cannot be used (broken characters; "
                                            "unpaired surrogates)"),
                        params={"reason": "text_invalid_chars"})


def validate_text(text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise BackendError("INVALID_INPUT", tr("生成する内容の文章が空です", "The text describing what to generate is empty"),
                           params={"reason": "text_empty"})
    t = text.strip()
    if len(t) > MAX_TEXT:
        raise BackendError("INVALID_INPUT", tr(f"文章が長すぎます（{len(t)} 文字。{MAX_TEXT} 文字まで）",
                                               f"The text is too long ({len(t)} characters; up to {MAX_TEXT})"),
                           params={"reason": "text_too_long", "max": MAX_TEXT})
    if not utf8_ok(t):           # 下書きを作る前に断る（以前は下書きを作った後に INTERNAL になった）
        raise text_invalid_chars()
    return t


# 生成 AI（TRELLIS-text-xlarge の CLIP）は英語の文章を前提にしている。かな・漢字などは意味の取りにくいバイト片の
# トークンになり、読まれる上限（77 トークン）も日本語では約 30 文字で超える（D-013 の「武器なし」は生成側が残す）。
# 止めずに注意だけを出す。全角の句読点や é などが少し混じっただけでは出さないように、割合で判定する
NON_ENGLISH_RATIO = 0.2


def text_notes(t: str) -> list[str]:
    """文章の注意（止めない）→ コードの一覧。今は ``prompt_non_english``（英字以外の文字が 2 割以上）だけ。"""
    chars = [c for c in t if not c.isspace()]
    if chars and sum(1 for c in chars if ord(c) > 127) / len(chars) >= NON_ENGLISH_RATIO:
        return ["prompt_non_english"]
    return []


def _image_size_error(w: int | None = None, h: int | None = None) -> BackendError:
    return BackendError("INVALID_INPUT",
                        tr(f"画像の大きさが範囲外です（{w}×{h}）", f"The image size is out of range ({w}×{h})") if w and h
                        else tr("画像の大きさが範囲外です", "The image size is out of range"),
                        params={"reason": "image_size", "min": MIN_IMAGE_SIDE, "max": MAX_IMAGE_SIDE,
                                "max_pixels": MAX_IMAGE_PIXELS})


def _check_image_size(w: int, h: int, check_min: bool = True) -> None:
    """一辺と画素数を確かめる。画像の全体を復号する前に呼ぶ（巨大な画像で数百 MB を確保しない。点検 C49）。
    check_min=False は上限だけ（copy_image。入力の検査は inspect_image が行う）。"""
    if (check_min and min(w, h) < MIN_IMAGE_SIDE) or max(w, h) > MAX_IMAGE_SIDE or w * h > MAX_IMAGE_PIXELS:
        raise _image_size_error(w, h)


@contextlib.contextmanager
def _open_image(path: str, check_min: bool = True):
    """Image.open（遅延読み込み）。大きさを確かめてから渡す。Pillow の DecompressionBombError（約 1.79 億画素超）は
    INVALID_INPUT / image_size にする（以前は想定外の例外 INTERNAL になっていた。点検 C49）。受け付ける範囲
    （MAX_IMAGE_PIXELS 以下）の DecompressionBombWarning は、大きさを自分で確かめているので出さない。"""
    from PIL import Image
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        try:
            im = Image.open(path, formats=IMAGE_FORMATS)
        except Image.DecompressionBombError:
            raise _image_size_error() from None
        with im:
            _check_image_size(*im.size, check_min=check_min)
            yield im


def wants_alpha(im) -> bool:
    """透明部分を持ちうる画像か（alpha のある mode、または透明色の指定 transparency（PNG の tRNS・GIF など）がある）。
    inspect_image と copy_image（生成と「背景除去を確認」に渡す画像）で同じ判定を使う（点検 C46・C54）。"""
    return im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info


def _gray16_to_8bit(im):
    """16 bit のグレースケール（I;16 系・I）→ 8 bit の L（透明色の指定があれば LA）。Pillow の convert("RGB") は 255 を
    超える値を 255 に切り詰めるので、ほぼ真っ白になる（点検 C45）。上位 8 bit を使い、透明色は元の 16 bit の値で判定する。
    mode I（32 bit）は 0〜65535 の範囲だけ受け付ける（それ以外は読めない画像として扱う）。"""
    import numpy as np
    from PIL import Image
    a = np.asarray(im)
    if im.mode == "I" and a.size and (int(a.min()) < 0 or int(a.max()) > 65535):
        raise ValueError(tr("16 bit を超える値の画像", "the image has values beyond 16 bits"))
    out = Image.fromarray((a.astype(np.uint32) >> 8).astype(np.uint8), "L")
    t = im.info.get("transparency")
    if isinstance(t, int):
        out = Image.merge("LA", (out, Image.fromarray(np.where(a == t, 0, 255).astype(np.uint8), "L")))
    if im.info.get("icc_profile"):
        out.info["icc_profile"] = im.info["icc_profile"]
    return out


def _to_rgb_or_rgba(im):
    """色のプロファイルを使わない単純な変換 → RGB（透明部分を持ちうる画像は RGBA）。すでにその mode なら同じ画像を
    返す（元の大きさの複製を作らない。FG03-R3。呼び出し側は返された画像を書き換えない）。"""
    if im.mode in _GRAY16_MODES:
        im = _gray16_to_8bit(im)
    mode = "RGBA" if wants_alpha(im) else "RGB"
    return im if im.mode == mode else im.convert(mode)


def _icc_to_srgb(im, rgba: bool, notes: list[str]):
    """埋め込みの色のプロファイル（ICC）で sRGB にする（点検 C50）→ RGB / RGBA。プロファイルが無い・使えないときは None
    （呼び出し側が単純な変換をする。使えないときは notes に icc_failed）。CMYK の JPEG・Display P3 の写真など。
    ImageCms が直接扱えない mode（P・PA・LA・RGBA など）は、プロファイルの色空間に合わせて先に変換する。"""
    import io
    icc = im.info.get("icc_profile")
    if not icc:
        return None
    from PIL import ImageCms
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        space = (src.profile.xcolor_space or "").strip().upper()
        if space == "RGB" and im.mode in ("RGB", "RGBA", "P", "PA", "L", "LA", "1"):
            base = im.convert("RGB")
        elif space == "GRAY" and im.mode in ("L", "LA", "1"):
            base = im.convert("L")
        elif space == "CMYK" and im.mode == "CMYK":
            base = im
        else:
            raise ValueError(tr(f"プロファイルの色空間 {space} と画像の mode {im.mode} が合わない",
                                f"the profile color space {space} does not match the image mode {im.mode}"))
        out = ImageCms.profileToProfile(base, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
        if out is None:
            raise ValueError(tr("色の変換の結果がない", "the color conversion returned no result"))
        if rgba:
            out.putalpha(_to_rgb_or_rgba(im).getchannel("A"))
        return out
    except (ImageCms.PyCMSError, OSError, ValueError, TypeError):
        notes.append("icc_failed")
        return None


def _shrink_for_work(im):
    """長辺を WORK_IMAGE_SIDE まで縮める（点検 C51）。TRELLIS は透明部分の無い画像を 1,024 px 以下に縮めてから背景を
    除くので結果は変わらない。透明部分のある画像は縮めずに切り抜いて 518 px にするので、縮めた後も切り抜きの範囲が
    518 px 以上残るときだけ縮める（小さな被写体の解像度を落とさない）。

    TRELLIS（preprocess_image・generate_glb.background_removed）は alpha が 1 画素でも 255 未満なら背景を除かない。
    縮めるとこの判定が変わることがある（255 に近い孤立した値が平均されて 255 になる）ので、縮めた後の alpha の最小を
    確かめ、変わったときは元の最小の画素に当たる画素の alpha を元の最小の値にする（すべて 255 の画像は 255 に戻す）。
    copy_image の前後で判定が同じになり、元の画像の inspect_image（has_alpha）に基づく u2net の確認・注意の文と
    食い違わない（FG03-R1）。元の大きさで作る配列は alpha 1 枚とその比較 1 枚だけにする（巨大な画像でメモリを
    使いすぎない。範囲は行・列の射影で求める。FG03-R3）。"""
    import numpy as np
    from PIL import Image
    w, h = im.size
    if max(w, h) <= WORK_IMAGE_SIDE:
        return im
    s = WORK_IMAGE_SIDE / max(w, h)
    size = (max(1, round(w * s)), max(1, round(h * s)))
    if im.mode != "RGBA":
        return im.resize(size, Image.Resampling.LANCZOS)
    a = np.asarray(im.getchannel("A"))
    amin = int(a.min())
    if amin < 255:                                   # TRELLIS は alpha をそのまま使う（背景を除かない）
        m = a >= ALPHA_OPAQUE
        rows, cols = np.flatnonzero(m.any(axis=1)), np.flatnonzero(m.any(axis=0))
        del m
        if len(rows) == 0:
            return im
        crop = int(max(int(cols[-1] - cols[0]), int(rows[-1] - rows[0])) * 1.2)
        if crop * s < TRELLIS_IMAGE_SIZE:
            return im
        y, x = divmod(int(np.argmin(a)), w)          # 元の最小の画素
    del a
    out = im.resize(size, Image.Resampling.LANCZOS)
    omin = int(np.asarray(out.getchannel("A")).min())
    if amin == 255 and omin < 255:
        out.putalpha(255)
    elif amin < 255 and omin == 255:
        p = (min(int((x + 0.5) * size[0] / w), size[0] - 1), min(int((y + 0.5) * size[1] / h), size[1] - 1))
        out.putpixel(p, out.getpixel(p)[:3] + (amin,))
    return out


def _flatten_alpha(im):
    """背景除去の強制（2026-10-17 追加機能 SF19）: RGBA → 透明部分を白（FLATTEN_BACKGROUND）に合成した RGB。TRELLIS は
    RGB の画像なら背景を除く（rembg・u2net）。alpha を捨てるだけ（convert("RGB")）では、透明な画素に隠れていた色
    （黒や編集の残り）がそのまま出るので、見た目どおりの白の上に合成する（半透明の影なども灰色になる）。"""
    from PIL import Image
    out = Image.new("RGB", im.size, FLATTEN_BACKGROUND)
    out.paste(im.convert("RGB"), mask=im.getchannel("A"))
    return out


FLATTEN_BACKGROUND = (255, 255, 255)


def load_input_image(path: str, notes: list[str] | None = None, flatten_alpha: bool = False):
    """入力画像 → 生成 AI に渡す画像（RGB、透明部分を持ちうる画像は RGBA）。生成（copy_image）と「背景除去を確認」で
    同じものを使う（点検 C54）。向きの EXIF を反映し、16 bit のグレースケールは 8 bit に（C45）、色のプロファイルが
    あれば sRGB に（C50）、長辺を WORK_IMAGE_SIDE まで縮める（C51）。止めない注意は notes に（icc_failed）。
    元の大きさの複製はできるだけ作らない（回す必要のない画像では exif_transpose を呼ばない。FG03-R3）。
    flatten_alpha（背景除去の強制。SF19。既定 False）は透明部分を白に合成した RGB にする（縮める前に。縮め方は RGB と同じ）。"""
    from PIL import ExifTags, ImageOps
    notes = [] if notes is None else notes
    with _open_image(path, check_min=False) as im:
        w, h = im.size
        if im.format == "JPEG" and max(w, h) > WORK_IMAGE_SIDE:
            # JPEG は 1/2・1/4・1/8 で復号できる（要求より小さくはならない）。巨大な写真で全体を復号しない
            s = WORK_IMAGE_SIDE / max(w, h)
            im.draft(None, (math.ceil(w * s), math.ceil(h * s)))
        im.load()
        if im.getexif().get(ExifTags.Base.Orientation, 1) in range(2, 9):
            im = ImageOps.exif_transpose(im)     # 向きが 1・無しでは何もせずに全体の複製を返すだけなので呼ばない
        rgba = wants_alpha(im)
        base = _gray16_to_8bit(im) if im.mode in _GRAY16_MODES else im
        out = _icc_to_srgb(base, rgba, notes)
        if out is None:
            out = _to_rgb_or_rgba(base)
    if flatten_alpha and out.mode == "RGBA":
        out = _flatten_alpha(out)
    return _shrink_for_work(out)


# 入力画像の止めない注意のコード（ログの文は image_note。画面の文はリソースの Quality_<コード>。下書きの注意になる）
IMAGE_NOTES = ("alpha_nearly_opaque", "icc_failed", "subject_edge", "subject_small", "subject_multiple")


def image_note(code: str) -> str:
    """入力画像の止めない注意のコード（IMAGE_NOTES）→ ログの文（表示言語）。"""
    return {
        "alpha_nearly_opaque": tr("透明部分はありますが、ほぼ全体が不透明なため、背景を除かずに生成します（背景ごと 3D になります。"
                                  "画像の欄の「この画像の詳細設定」で「背景除去を強制する」をオンにすると背景を除けます）",
                                  "The image has transparent areas, but almost all of it is opaque, so it is generated "
                                  "without removing the background (the background becomes part of the 3D model; you can "
                                  "remove it with \"Force background removal\" in \"Advanced settings for this image\")"),
        "icc_failed": tr("色のプロファイルを読めなかったため、色の補正をせずに使います",
                         "The color profile could not be read, so the image is used without color correction"),
        # 被写体の検査（2026-10-17 追加機能 SF21。bg_preview.subject_checks。止めない）
        "subject_edge": tr("被写体が画像の端に接しています（足や頭などが切れているかもしれません）",
                           "The subject touches the edge of the image (the feet, head or similar may be cut off)"),
        "subject_small": tr("被写体が小さいため、生成 AI に渡す前に大きく拡大されます（細部がぼやけることがあります）",
                            "The subject is small, so it is enlarged a lot before being passed to the generation AI "
                            "(details may be blurry)"),
        "subject_multiple": tr("離れた物体が複数あります（キャラクターのほかに別の物が残っているかもしれません）",
                               "There are several separate objects (something besides the character may remain)"),
    }[code]
# 透明部分（alpha）から決まる注意。背景除去を強制した画像（alpha を使わない。SF19）では外す
ALPHA_NOTES = ("alpha_nearly_opaque", "subject_edge", "subject_small", "subject_multiple")


def _bg_preview():
    """bg_preview.py（app/backend。被写体の検査 subject_checks を共用する。numpy と Pillow だけで読み込める）。"""
    try:
        import bg_preview
    except ImportError:                      # app/backend が sys.path に無い呼び出し方
        import importlib.util
        spec = importlib.util.spec_from_file_location("bg_preview", BG_PREVIEW_SCRIPT)
        bg_preview = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bg_preview)
    return bg_preview


def inspect_image(path) -> dict:
    """画像の形式を確かめる（計画書 4.2「対応画像形式の確認」）。中身はログに出さない。

    透明部分の判定は copy_image と同じ（wants_alpha。PNG の tRNS も含む。点検 C46）。``warnings`` は止めない注意の
    コード（``alpha_nearly_opaque``: 透明部分はあるが、ほぼ全体が不透明で、TRELLIS が背景を除かない。点検 C09。
    透明部分のある画像では被写体の検査 ``subject_edge``・``subject_small``・``subject_multiple`` も。SF21。詳細は
    ``subject``）。透明部分の無い画像の被写体の検査は、背景を除いた後（「背景除去を確認」・アプリ側の背景除去）に行う。"""
    from PIL import UnidentifiedImageError
    if not isinstance(path, str) or not path or not os.path.isfile(path):
        raise BackendError("INVALID_INPUT", tr("画像ファイルが見つかりません", "The image file was not found"),
                           params={"reason": "image_not_found"})
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTS:
        raise BackendError("INVALID_INPUT", tr(f"対応していない画像形式です（{ext}）", f"Unsupported image format ({ext})"),
                           params={"reason": "image_format", "allowed": sorted(IMAGE_EXTS)})
    size = os.path.getsize(path)
    if size > MAX_IMAGE_BYTES:
        raise BackendError("INVALID_INPUT", tr(f"画像ファイルが大きすぎます（{size:,} B）",
                                               f"The image file is too large ({size:,} bytes)"),
                           params={"reason": "image_too_large", "max_bytes": MAX_IMAGE_BYTES})
    try:
        with _open_image(path) as im:      # 大きさは全体を復号する前に確かめる（C49）
            w, h = im.size
            fmt = im.format
            has_alpha = wants_alpha(im)
            coverage = subject = None
            if has_alpha:
                alpha = _to_rgb_or_rgba(im).getchannel("A")
                hist = alpha.histogram()
                total = w * h
                has_alpha = sum(hist[:255]) > 0          # すべて不透明なら TRELLIS は透明部分なしとして背景を除く
                coverage = sum(hist[ALPHA_OPAQUE:]) / total if total else 0.0
                if has_alpha and coverage >= 0.005:
                    # 被写体の検査（SF21）: TRELLIS は透明部分のある画像をそのまま切り抜くので、この alpha で決まる
                    # （copy_image が縮めるのは切り抜きが 518 px 以上残るときだけなので、「小さい」の判定も変わらない）
                    bgp = _bg_preview()
                    subject = bgp.subject_checks(bgp.mask_from_alpha(alpha), removed=False)
                del alpha
            else:
                if fmt == "JPEG":
                    im.draft(None, (max(1, w // 8), max(1, h // 8)))    # 壊れていないかを確かめるだけなので縮めて復号
                im.load()
    except BackendError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise BackendError("INVALID_INPUT", tr(f"画像として読めません: {type(e).__name__}",
                                               f"The file cannot be read as an image: {type(e).__name__}"),
                           params={"reason": "image_unreadable"})
    if has_alpha and coverage is not None and coverage < 0.005:
        raise BackendError("INVALID_INPUT", tr("画像の不透明な部分（キャラクター）がほとんどありません",
                                               "The image has almost no opaque area (character)"),
                           params={"reason": "image_empty"})
    notes = ["alpha_nearly_opaque"] if has_alpha and coverage is not None and coverage >= NEARLY_OPAQUE_COVERAGE else []
    notes += list((subject or {}).get("warnings") or [])
    out = {"format": fmt, "size": [w, h], "bytes": size, "has_alpha": bool(has_alpha), "coverage": coverage,
           "warnings": notes}
    if subject is not None:
        out["subject"] = subject
    return out


def normalize_images(inp: dict) -> list[dict]:
    """画像入力（1〜4 枚）→ [{"path", "label"}]。``image_paths``・``image_labels``（無ければ ``image_path`` 1 枚）。"""
    paths_ = inp.get("image_paths")
    if paths_ is None:
        paths_ = [inp["image_path"]] if inp.get("image_path") else []
    if not isinstance(paths_, list) or not all(isinstance(p, str) and p for p in paths_):
        raise BackendError("INVALID_INPUT", tr("画像の指定が不正です", "The image specification is invalid"),
                           params={"reason": "image_not_found"})
    if not 1 <= len(paths_) <= MAX_IMAGES:
        raise BackendError("INVALID_INPUT", tr(f"画像の枚数が範囲外です（{len(paths_)} 枚。1〜{MAX_IMAGES} 枚）",
                                               f"The number of images is out of range ({len(paths_)}; 1 to {MAX_IMAGES})"),
                           params={"reason": "image_count", "max": MAX_IMAGES})
    labels = inp.get("image_labels") or []
    if labels and (not isinstance(labels, list) or len(labels) != len(paths_)):
        raise BackendError("INVALID_INPUT", tr("画像の名前の数が画像の枚数と違います",
                                               "The number of image names does not match the number of images"),
                           params={"reason": "image_labels"})
    # 背景除去の強制（画像ごと。SF19。省略・false は今までどおり）
    force = inp.get("image_force_bg_removal") or []
    if force and (not isinstance(force, list) or len(force) != len(paths_) or not all(isinstance(x, bool) for x in force)):
        raise BackendError("INVALID_INPUT", tr("背景除去の強制の指定が画像の枚数と合いません",
                                               "The \"Force background removal\" settings do not match the number of images"),
                           params={"reason": "image_options"})
    seen = set()
    out = []
    for i, p in enumerate(paths_):
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            raise BackendError("INVALID_INPUT", tr(f"同じ画像が 2 回指定されています（{i + 1} 枚目）",
                                                   f"The same image is specified twice (image {i + 1})"),
                               params={"reason": "image_duplicate", "index": i + 1})
        seen.add(key)
        lab = labels[i] if labels else None
        lab = (lab if isinstance(lab, str) else "").strip() or f"image {i + 1}"
        if len(lab) > LABEL_MAX or any(ord(c) < 32 for c in lab) or not utf8_ok(lab):      # 壊れた文字も（R10B-03）
            raise BackendError("INVALID_INPUT", tr(f"画像の名前が長すぎるか、使えない文字があります（{i + 1} 枚目。{LABEL_MAX} 文字まで）",
                                                   f"The name of image {i + 1} is too long or contains characters that "
                                                   f"cannot be used (up to {LABEL_MAX} characters)"),
                               params={"reason": "image_label", "index": i + 1, "max": LABEL_MAX})
        out.append({"path": p, "label": lab, **({"force_bg_removal": True} if force and force[i] else {})})
    return out


def u2net_available(trellis_root: str | None) -> bool:
    """背景の自動除去（TRELLIS の preprocess_image が rembg の u2net を使う。D-020）のモデルが実行環境にあるか。"""
    from .providers.trellis import find_u2net
    return find_u2net(trellis_root) is not None


def needs_bg_removal(im: dict) -> bool:
    """背景を除く画像か（透明部分が無い、または背景除去を強制した。im は normalize_images の項目に meta を足したもの）。"""
    return not im["meta"]["has_alpha"] or bool(im.get("force_bg_removal"))


def background_model_missing(trellis_root: str | None, has_opaque: bool) -> BackendError:
    """背景を除く画像があるのに、背景除去のモデルが無いときのエラー。透明部分の無い画像があれば今までどおり
    background_not_transparent、透明部分のある画像の強制（SF19）だけなら background_forced_missing。"""
    if has_opaque:
        return background_not_transparent(trellis_root)
    from .providers.trellis import u2net_expected
    return BackendError("INVALID_INPUT", tr("背景除去を強制した画像があります（背景の自動除去のモデル u2net が未取得）",
                                            "An image has \"Force background removal\" turned on (the automatic background "
                                            "removal model u2net has not been downloaded)"),
                        params={"reason": "background_forced_missing", "expected": u2net_expected(trellis_root)})


def background_not_transparent(trellis_root: str | None) -> BackendError:
    """透明部分の無い画像で、背景除去のモデルが無いときのエラー（取得の案内つき）。"""
    from .providers.trellis import u2net_expected
    return BackendError("INVALID_INPUT", tr("背景が透明ではない画像です（背景の自動除去のモデル u2net が未取得）",
                                            "The image does not have a transparent background (the automatic background "
                                            "removal model u2net has not been downloaded)"),
                        params={"reason": "background_not_transparent", "expected": u2net_expected(trellis_root)})


def validate_yaw(v) -> int:
    """変換の前の回転（度）を検査する → 0 / 180。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v not in YAW_CHOICES:
        raise BackendError("INVALID_INPUT", tr(f"回転の角度が不正です: {v!r}（{' / '.join(map(str, YAW_CHOICES))}）",
                                               f"Invalid rotation angle: {v!r} ({' / '.join(map(str, YAW_CHOICES))})"),
                           params={"reason": "yaw", "allowed": list(YAW_CHOICES)})
    return int(v)


def conversion_axes(gen_axes: dict | None, yaw_deg: int) -> dict | None:
    """生成 AI の向きの指定（``GenerationOutput.axes``）に、利用者が選んだ y 軸回りの回転を足す（gdconvert の ``AxisConfig.yaw_deg``）。"""
    if not yaw_deg:
        return gen_axes
    axes = dict(gen_axes or {})
    axes["yaw_deg"] = (float(axes.get("yaw_deg") or 0.0) + yaw_deg) % 360
    return axes


def copy_image(src: str, dst: str, flatten_alpha: bool = False) -> list[str]:
    """生成プロセス（と「背景除去を確認」）に渡す PNG を作る（load_input_image。透明部分はそのまま。flatten_alpha は
    背景除去の強制で白に合成した RGB。SF19）→ 止めない注意のコード。

    PNG には色のプロファイルを書かない（中身は sRGB。TRELLIS は numpy で読み、プロファイルを見ない。点検 C50）。"""
    notes: list[str] = []
    load_input_image(src, notes, flatten_alpha=flatten_alpha).save(dst, "PNG", icc_profile=None)
    return notes


# ---------------------------------------------------------------------------
# GLB の取り込み（外部ファイルの埋め込み。点検 C56）
# ---------------------------------------------------------------------------
_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".ktx2": "image/ktx2"}


def _read_glb(data: bytes) -> tuple[dict, bytes | None] | None:
    """GLB → (JSON, BIN チャンク)。GLB として読めなければ None（変換が理由つきの INVALID_MODEL にする）。"""
    if len(data) < 20 or data[:4] != b"glTF":
        return None
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2:
        return None
    pos, doc, binchunk = 12, None, None
    while pos + 8 <= min(length, len(data)):
        clen, ctype = struct.unpack_from("<I4s", data, pos)
        body = data[pos + 8:pos + 8 + clen]
        if ctype == b"JSON":
            try:
                doc = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return None
        elif ctype == b"BIN\x00" and binchunk is None:
            binchunk = body
        pos += 8 + clen
    return (doc, binchunk) if isinstance(doc, dict) else None


def _glb_externals(doc: dict, base_dir: str) -> list[tuple[str, int, str]]:
    """外部ファイルを参照する buffers・images → [(種類, 番号, パス)]。パスは gdconvert.gltf と同じ規則
    （resolve_external_uri）で GLB のフォルダーから。GLB のフォルダーからの相対パスでない参照（絶対パス・ドライブ名・
    UNC・スキーム）は、ファイルに触る前に INVALID_INPUT（glb_external_unsafe。点検 R02B-01）。"""
    from gdconvert.gltf import GltfError, resolve_external_uri
    out = []
    for kind in ("buffers", "images"):
        items = doc.get(kind)
        for i, it in enumerate(items if isinstance(items, list) else []):
            uri = it.get("uri") if isinstance(it, dict) else None
            if isinstance(uri, str) and not uri.startswith("data:"):
                try:
                    out.append((kind, i, resolve_external_uri(base_dir, uri)))
                except GltfError:
                    raise BackendError("INVALID_INPUT", tr("GLB が、GLB のフォルダーからの相対パスではない形（絶対パス・別のドライブ・ネットワーク上の場所など）で"
                                                           "ファイルを参照しています",
                                                           "The GLB refers to a file with a path that is not relative to the GLB's folder (an absolute path, another "
                                                           "drive, a network location, etc.)"),
                                       params={"reason": "glb_external_unsafe"}) from None
    return out


def check_glb_external(path: str) -> None:
    """取り込む GLB が参照する外部ファイル（画像・.bin）が元の場所にあるかを、下書きを作る前に確かめる。"""
    try:
        with open(path, "rb") as f:
            parsed = _read_glb(f.read())
    except OSError:
        return                                   # 読めないファイルは変換が理由つきで止める
    if parsed is None:
        return
    missing = [p for _k, _i, p in _glb_externals(parsed[0], os.path.dirname(os.path.abspath(path))) if not os.path.isfile(p)]
    if missing:
        raise BackendError("INVALID_INPUT", tr(f"GLB が参照する外部ファイルが見つかりません（{len(missing)} 個）",
                                               f"External files referenced by the GLB were not found ({len(missing)})"),
                           params={"reason": "glb_external_missing", "count": len(missing)})


def import_glb(src: str, dst: str) -> int:
    """取り込んだ GLB を下書きに書く → 埋め込んだ外部ファイルの数。外部ファイル（画像・.bin）を参照する GLB は、元の
    フォルダーから読んで data: URI として埋め込んだ自己完結の GLB にする（下書きのフォルダーへのコピーで参照が切れて
    読めなくなっていた。相対位置へのコピーは ../ を含む参照で下書きの外を指すので行わない）。外部参照の無い GLB と、
    GLB として読めないファイルはそのままコピーする。"""
    with open(src, "rb") as f:
        data = f.read()
    parsed = _read_glb(data)
    ext = _glb_externals(parsed[0], os.path.dirname(os.path.abspath(src))) if parsed else []
    if not ext:
        shutil.copyfile(src, dst)
        return 0
    doc, binchunk = parsed
    for kind, i, p in ext:
        if not os.path.isfile(p):
            raise BackendError("INVALID_INPUT", tr("GLB が参照する外部ファイルが見つかりません",
                                                   "An external file referenced by the GLB was not found"),
                               params={"reason": "glb_external_missing", "count": 1})
        with open(p, "rb") as f:
            raw = f.read()
        it = doc[kind][i]
        mime = (it.get("mimeType") or _IMAGE_MIME.get(os.path.splitext(p)[1].lower())) if kind == "images" else None
        it["uri"] = f"data:{mime or 'application/octet-stream'};base64," + base64.b64encode(raw).decode("ascii")
    js = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    body = struct.pack("<I4s", len(js), b"JSON") + js
    if binchunk is not None:
        binchunk += b"\0" * (-len(binchunk) % 4)
        body += struct.pack("<I4s", len(binchunk), b"BIN\x00") + binchunk
    with open(dst, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body)
    return len(ext)


# ---------------------------------------------------------------------------
# 事前の確認
# ---------------------------------------------------------------------------
TEXTURE_COMPILER = "TextureCompiler.exe"


def require_texture_compiler(gd: str | None) -> None:
    """Grim Dawn のフォルダーに TextureCompiler.exe（変換で .tex を作る）が無ければ GAME_NOT_FOUND
    （reason ``texture_compiler_missing``）。作業プロセスの変換（worker_tasks.task_convert）も同じ確認をする。"""
    tool = os.path.join(gd or "", TEXTURE_COMPILER)
    if not gd or not os.path.isfile(tool):
        raise BackendError("GAME_NOT_FOUND", tr(f"Grim Dawn のフォルダーに {TEXTURE_COMPILER} がありません（変換で .tex を作るのに必要）",
                                                f"{TEXTURE_COMPILER} is not in the Grim Dawn folder (needed to make .tex "
                                                "files during conversion)"),
                           params={"reason": "texture_compiler_missing", "missing": TEXTURE_COMPILER, "path": tool})


def preflight(svc, kind: str, gen: dict, ctx=None) -> tuple[str, list[str]]:
    """生成・変換を始める前に止まる理由を確かめる → (Grim Dawn のフォルダー, 警告)。

    kind ``dump`` は保存した生成結果からの GLB の作り直し（generate_glb.py --from-dump と同じ名前。candidates）: 3D 生成の
    モデルを読み込まないので、生成 AI のモデル・部品（項目 model_*）と CLIP の注意文への同意（clip_consent）では止めない
    （実行環境・ソース・生成スクリプトと GPU は確かめる。点検 R10A-05）。"""
    from gdconvert.refs import missing_refs
    s = svc.settings.get()
    warnings: list[str] = []
    gd = resolve_game_dir(s)
    if gd is None:
        raise BackendError("GAME_NOT_FOUND", tr("Grim Dawn のインストール先が見つかりません（変換で .tex を作るのに必要）",
                                                "The Grim Dawn install folder was not found (needed to make .tex files "
                                                "during conversion)"))
    missing = missing_refs(svc.refs_dir())
    if missing:
        raise BackendError("REFS_MISSING", tr("基準メッシュがありません（設定画面の「ゲームから準備する」で作成）",
                                              "The reference meshes are missing (create them with \"Prepare from the "
                                              "game\" in Settings)"),
                           params={"missing": missing})
    # あるが読めない基準ファイル（点検 R03A-01）。開けないもの（ほかのアプリが共有なしで開いている・アクセス権が無い）も
    # 想定外のエラーにせず REFS_MISSING で止める（点検 D2_backend-03）
    corrupt, locked, locked_error = envcheck.refs_problems(svc.refs_dir())
    if corrupt:
        raise BackendError("REFS_MISSING", tr("基準メッシュが壊れています（設定画面の「ゲームから準備する」で取り出し直す）",
                                              "The reference meshes are corrupted (extract them again with \"Prepare "
                                              "from the game\" in Settings)"),
                           params={"reason": "refs_corrupt", "corrupt": corrupt})
    if locked:
        # 文は変換（worker_tasks.task_convert）の開けないときと同じ（2026-11-04 退行の確認 2 回目 B1）
        from .worker_tasks import refs_unreadable_message
        raise BackendError("REFS_MISSING", refs_unreadable_message(), locked_error,
                           params={"reason": "refs_unreadable", "unreadable": locked})
    # 変換の最後に .tex を作るゲーム付属のツール。無いと GPU での生成と数分の変換の後で失敗していた（点検 C55）。
    # 生成・変換し直し・装備一覧からの変換し直しのどれもここを通る
    require_texture_compiler(gd)
    if kind in ("image", "text", "dump"):
        prov = providers.get(gen["provider"])
        for item in prov.check(s, None if kind == "dump" else kind):
            if kind == "dump" and (item.id.startswith("model_") or item.id == "clip_consent"):
                continue
            if item.status in ("error", "missing"):
                raise BackendError(item.code or "LIBRARY_MISSING", item.message, params=item.params)
        # GPU の判定は環境チェックと同じ（envcheck.gpu_items。判定に使う GPU・ドライバー・compute capability・GPU メモリ。
        # 点検 C03・C32）。生成の子プロセスも同じ GPU を使う（providers.trellis.runtime_env → envcheck.gpu_env）
        gitems = {c.id: c for c in envcheck.gpu_items(envcheck.gpu_info())}
        for c in gitems.values():
            if c.status in ("error", "missing"):
                raise BackendError(c.code or "GPU_NOT_FOUND", c.message, params=c.params)
        mem = gitems["gpu_memory"].params
        if gitems["gpu_memory"].status == "warning":
            warnings.append(tr(f"GPU メモリが 16 GB 未満です（{mem['total_mib']} MiB）。生成に失敗することがあります",
                               f"GPU memory is less than 16 GB ({mem['total_mib']} MiB). Generation may fail")
                            + ("" if gen.get("low_vram") else tr("（詳細設定の「低 VRAM モード」で GPU メモリの最大の使用量を下げられます）",
                                                                 " (\"Low VRAM mode\" in Advanced settings lowers the peak "
                                                                 "GPU memory use)")))
        if "gpu_compute" in gitems and gitems["gpu_compute"].status == "warning":
            warnings.append(gitems["gpu_compute"].message)
        if gitems["gpu"].params.get("order_unknown"):   # 番号の GPU を CUDA に確かめられない（点検 D2_backend-01・04）
            warnings.append(gitems["gpu"].message)
        free = mem["total_mib"] - mem["used_mib"]
        if free < min(VRAM_FREE_WARN_MIB, mem["total_mib"] - VRAM_FREE_SLACK_MIB):
            warnings.append(tr(f"GPU メモリの空きが少なくなっています（空き {free} MiB）。ゲームなど GPU を使うアプリを閉じてください",
                               f"Free GPU memory is low ({free} MiB free). Close games and other apps that use the GPU"))
        if envcheck.game_running():
            warnings.append(tr("Grim Dawn が起動中です。GPU を取り合うため、生成が遅くなるか失敗することがあります",
                               "Grim Dawn is running. It competes for the GPU, so generation may be slow or fail"))
    return gd, warnings


# ---------------------------------------------------------------------------
# ジョブ
# ---------------------------------------------------------------------------
def summarize_generate(params: dict) -> dict:
    inp = params.get("input") or {}
    out = {"kind": inp.get("kind"), "provider": (params.get("settings") or {}).get("provider"),
           "replace_equipment_id": params.get("replace_equipment_id")}
    return out


_MAINT_JOBS = ("setup_remove", "setup_repair")


def _setup_busy(svc, what: str) -> BackendError | None:
    """列 setup の実行中・順番待ちなら BUSY。TRELLIS 実行環境の削除・修復（設定画面）だけなら reason = maint_running、
    初回セットアップを含めば setup_running（画面の文を、利用者がしている操作に合わせる。点検 U-D2-R5）。
    what はできないことの文（tr で表示言語にしたもの。日本語は文の後ろ、英語は文の先頭に置く）。"""
    active = svc.jobs.active("setup")
    if not active:
        return None
    if all(j.type in _MAINT_JOBS for j in active):
        return BackendError("BUSY", tr(f"TRELLIS 実行環境の照合し直し・入れ直し・削除の実行中は{what}",
                                       f"{what} while the TRELLIS runtime is being re-verified, reinstalled or removed"),
                            params={"reason": "maint_running"})
    return BackendError("BUSY", tr(f"初回セットアップ（TRELLIS 実行環境の取得）の実行中は{what}",
                                   f"{what} while the first-run setup (TRELLIS runtime download) is running"),
                        params={"reason": "setup_running"})


def run_generate(svc, ctx, params: dict) -> dict:
    s = svc.settings.get()
    inp = params.get("input") or {}
    kind = inp.get("kind")
    if kind not in ("image", "text", "glb"):
        raise BackendError("INVALID_INPUT", tr(f"入力の種類が不正です: {kind}（image / text / glb）",
                                               f"Invalid input kind: {kind} (image / text / glb)"))
    busy = _setup_busy(svc, tr(" 3D 生成を始められません", "3D generation cannot be started")) if kind != "glb" else None
    if busy:
        raise busy
    gen = validate_generation({**s["generation"], **(params.get("settings") or {})})
    # 入力（文章・画像や GLB のパスとファイル名）はログに出さない。以後のログ・子プロセスの出力から置き換える
    path = inp.get("glb_path") if kind == "glb" else None
    img_paths = [p for p in (inp.get("image_paths") or [inp.get("image_path")]) if isinstance(p, str) and p] if kind == "image" else []
    secrets = [x for x in [inp.get("text") if kind == "text" else None, path, os.path.basename(path) if isinstance(path, str) else None]
               + img_paths + [os.path.basename(p) for p in img_paths] if isinstance(x, str) and x]
    for x in secrets:
        redact.add_secret(x)
    try:
        text = image_meta = None
        if kind == "text":
            text = validate_text(inp.get("text"))
        elif kind == "image":
            images = normalize_images(inp)
            for i, im in enumerate(images):
                try:
                    im["meta"] = inspect_image(im["path"])
                except BackendError as e:
                    e.params = {**e.params, "index": i + 1}
                    raise
            # TRELLIS 実行環境そのものが無いときは u2net の有無を問わない（文章の入力と同じく、preflight が
            # LIBRARY_MISSING で止める。下書きを作る前。点検 R10A-02）
            root = resolve_trellis_root(s)
            if root is not None and any(needs_bg_removal(im) for im in images) and not u2net_available(root):
                raise background_model_missing(root, any(not im["meta"]["has_alpha"] for im in images))
            if len(images) > 1 and gen["provider"] == "trellis" and not providers.get("trellis").multi_image_supported():
                raise BackendError("INVALID_INPUT", tr("生成スクリプトが複数画像にまだ対応していません（1 枚で生成してください）",
                                                       "The generation script does not support multiple images yet "
                                                       "(generate with one image)"),
                                   params={"reason": "multi_image_unsupported"})
            image_meta = images
        elif not path or not os.path.isfile(path) or not path.lower().endswith(".glb"):
            raise BackendError("INVALID_INPUT", tr("GLB ファイルが見つかりません", "The GLB file was not found"),
                               params={"reason": "glb_not_found"})
        else:
            check_glb_external(path)            # 外部ファイルの参照が切れていれば下書きを作る前に止める（点検 C56）
        if not (gen.get("bg_app_removal") and kind == "image" and any(needs_bg_removal(im) for im in image_meta)):
            # アプリ側の背景除去と後処理（SF20）は、背景を除く画像があるときだけ使う。使わない生成（文章・GLB・透明部分を
            # そのまま使う画像だけ・アプリ側がオフ）では既定に戻し、下書き・装備の記録（generation.settings）とログに
            # 使ったように残さない（点検 UB-R2。既定では何も変わらない）
            gen = {**gen, "bg_app_removal": False, **{k: False for k in BG_OPTION_KEYS}}
        return _run_generate(svc, ctx, params, s, kind, gen, text, image_meta, inp)
    finally:
        for x in secrets:
            redact.remove_secret(x)


def _run_generate(svc, ctx, params, s, kind, gen, text, image_meta, inp) -> dict:
    ctx.progress(0.0, "generate_check")
    gd, warnings = preflight(svc, kind, gen, ctx)
    for w in warnings:
        ctx.warn(w)
    if kind == "text" and "prompt_non_english" in text_notes(text):
        ctx.warn(tr("文章に英語以外の文字が多く含まれています。生成 AI は英語の文章を前提にしているため、意図どおりの形に"
                    "ならないことがあります（日本語では約 30 文字で、生成 AI が読む長さの上限を超えます）",
                    "The text contains many non-English characters. The generation AI expects English text, so the "
                    "shape may not come out as intended (in Japanese, about 30 characters already exceed the length "
                    "the generation AI reads)"), code="prompt_non_english")
    prov = providers.get(gen["provider"]) if kind in ("image", "text") else None
    draft = svc.drafts.create(kind, prov.id if prov else None, gen, params.get("replace_equipment_id"), ctx.job.id)
    try:
        ctx.job.emit("draft", draft_id=draft.id)
        # 画像はファイル名・名前（ラベル）を出さず、枚数と形式・画素数・透明の有無だけ
        what = tr(f"、文章 {redact.fingerprint(text)}", f", text {redact.fingerprint(text)}") if text else ""
        if image_meta:
            imgs = _images_log(image_meta)
            what += tr(f"、画像 {len(image_meta)} 枚: {imgs}",
                       f", {len(image_meta)} image{'' if len(image_meta) == 1 else 's'}: {imgs}")
            if len(image_meta) > 1:
                what += tr(f"、モード {gen['multi_mode']}", f", multi-image mode {gen['multi_mode']}")
        ctx.info(tr(f"下書き {draft.id} を作成（入力: {kind}{what}）", f"Created draft {draft.id} (input: {kind}{what})"))
        if kind == "image":
            recs = []
            for i, im in enumerate(image_meta, 1):
                rel = f"input/input_{i}.png"
                force = bool(im.get("force_bg_removal"))
                # 背景除去を強制した画像（SF19）は alpha を使わないので、alpha から決まる注意（C09・SF21）を外す
                meta_notes = [c for c in im["meta"].get("warnings") or [] if not (force and c in ALPHA_NOTES)]
                notes = meta_notes + copy_image(im["path"], draft.path(*rel.split("/")))
                for code in notes:
                    if code in IMAGE_NOTES:
                        ctx.warn(tr(f"{i} 枚目の画像: {image_note(code)}", f"Image {i}: {image_note(code)}"), code=code)
                rec = {"file": rel, "label": im["label"], "original_name": os.path.basename(im["path"]),
                       "sha256": _sha256(im["path"]), **im["meta"], "warnings": notes}
                if force:
                    # input_{i}.png は元の画像（透明部分つき。再生成で強制を外せるように）、生成には白に合成した RGB を渡す。
                    # 「背景除去を確認」（run_bg_preview）と同じ copy_image で作る（C54）
                    if im["meta"]["has_alpha"]:
                        rec["generation_file"] = f"input/generation_{i}.png"
                        copy_image(im["path"], draft.path(*rec["generation_file"].split("/")), flatten_alpha=True)
                        rec.pop("subject", None)
                        ctx.info(tr(f"{i} 枚目の画像: 背景除去を強制します（透明部分を白で塗りつぶしてから背景を除きます）",
                                    f"Image {i}: forcing background removal (transparent areas are filled with white, "
                                    "then the background is removed)"))
                    rec["force_bg_removal"] = True
                recs.append(rec)
            # アプリ側の背景除去が失敗しても、失敗の記録として残す下書き（点検 C104）に入力画像の記録が残るように先に置く
            # （_app_bg_removal は recs をその場で書き換える。点検 UB-R3）
            draft.data["input"]["images"] = recs
            draft.data["input"]["image"] = recs[0]                 # 1 枚目（以前の形式と同じ場所）
            draft.data["input"]["warnings"] = sorted({c for r in recs for c in r["warnings"]})
            if gen.get("bg_app_removal"):
                _app_bg_removal(ctx, s, gen, draft, image_meta, recs)
            # 入力画像の注意（下書きの注意 DraftWarnings に出す。生成の記録の warnings の先頭に入れる。_generate）
            draft.data["input"]["warnings"] = sorted({c for r in recs for c in r["warnings"]})
        elif kind == "text":
            draft.data["input"]["text"] = text
        else:
            dst = os.path.join(draft.generate_dir, "input.glb")
            if import_glb(inp["glb_path"], dst):
                ctx.info(tr("GLB が参照する外部ファイル（画像・.bin）を、下書きの GLB に埋め込みました",
                            "Embedded the external files referenced by the GLB (images, .bin) into the draft's GLB"))
            draft.data["input"]["glb"] = {"file": "generate/input.glb", "original_name": os.path.basename(inp["glb_path"]),
                                          "sha256": _sha256(dst)}
        draft.data["status"] = "generating" if prov else "converting"
        svc.drafts.save(draft)

        # --- 3D 生成 -------------------------------------------------------------------------
        if prov:
            glb = _generate(svc, ctx, prov, s, kind, gen, text, draft)
        else:
            glb = os.path.join(draft.generate_dir, "input.glb")
            draft.data["generation"] = {"status": "skipped", "glb": "generate/input.glb", "files": {}, "warnings": []}
            draft.data["model_id"] = None
            svc.drafts.save(draft)

        # --- 変換 ------------------------------------------------------------------------------
        _convert(svc, ctx, draft, glb, gd, gen, CONV_RANGE if prov else (0.05, 1.0))
        draft.data["status"] = "ready"
        svc.drafts.save(draft)
        ctx.info(tr(f"下書き {draft.id} の準備ができました", f"Draft {draft.id} is ready"))
        return {"draft": draft.summary()}
    except JobCancelled:
        svc.drafts.delete(draft.id)
        raise
    except BackendError as e:
        draft.data["status"] = "failed"
        svc.drafts.add_error(draft, ctx.job.stage or "", e.code, e.message)
        e.params = {**e.params, "draft_id": draft.id}
        try:
            svc.drafts.save(draft)
        except (OSError, ValueError):    # 保存の失敗で分類済みのエラーを隠さない（点検 R10B-01。UTF-8 にできない文字も。R10B-03）
            pass
        raise
    except Exception as e:      # noqa: BLE001  想定外の例外（OSError など）も下書きを失敗として記録する（2026-10-17 監査 C104）
        err = BackendError("INTERNAL", f"{type(e).__name__}: {e}", failure_text(traceback.format_exc(), 30),
                           {"draft_id": draft.id})
        draft.data["status"] = "failed"
        svc.drafts.add_error(draft, ctx.job.stage or "", err.code, err.message)
        try:
            svc.drafts.save(draft)
        except (OSError, ValueError):
            pass
        raise err from e


def _app_bg_removal(ctx, s, gen, draft, image_meta, recs) -> None:
    """アプリ側の背景除去（2026-10-17 追加機能 SF20。詳細設定。既定オフ）: 背景を除く画像（透明部分が無い・背景除去を
    強制した）を、生成の前に TRELLIS 実行環境の Python（bg_preview.py remove。「背景除去を確認」と同じ処理）で背景を
    除いた RGBA（切り抜きの前）にして input/generation_<n>.png に書く。TRELLIS は透明部分のある画像の背景を除かずに
    切り抜くだけなので、後処理（post_process_mask・alpha_matting・decontaminate）を使える。被写体の検査（SF21）の
    注意も、背景を除いた結果で足す。透明部分をそのまま使う画像は何もしない。"""
    from .providers.trellis import find_u2net
    targets = [(i, r) for i, (im, r) in enumerate(zip(image_meta, recs), 1) if needs_bg_removal(im)]
    if not targets:
        return
    root = resolve_trellis_root(s)
    f = find_u2net(root)
    if root is None or f is None:
        # run_generate・run_bg_preview と同じ判定（強制した透明な画像だけなら background_forced_missing。点検 UB-R4）
        raise background_model_missing(root, any(not im["meta"]["has_alpha"] for im in image_meta))
    opts = {k[len("bg_"):]: True for k in BG_OPTION_KEYS if gen.get(k)}
    items = []
    for i, r in targets:
        src = r.get("generation_file") or r["file"]
        items.append({"index": i, "image": draft.path(*src.split("/")),
                      "out": draft.path("input", f"generation_{i}.png")})
    post = ""
    if opts:
        names = tr("・", " / ").join(sorted(opts))
        post = tr(f"、後処理: {names}", f", post-processing: {names}")
    ctx.info(tr(f"背景をアプリ側で除きます（{len(items)} 枚、rembg u2net{post}）",
                f"Removing the background in the app ({len(items)} image{'' if len(items) == 1 else 's'}, "
                f"rembg u2net{post})"))
    py = os.path.join(root, "venv", "Scripts", "python.exe")
    res = ctx.run_worker("remove", {"images": items, "u2net": f["path"], "u2net_md5_ok": f["md5_ok"], "rembg": opts},
                         (0.0, 0.01), default_error="GENERATION_FAILED", python=py, script=BG_PREVIEW_SCRIPT, env={"U2NET_HOME": f["home"]})
    done = {it.get("index"): it for it in res.get("images") or []}
    for i, r in targets:
        it = done.get(i) or {}
        r["generation_file"] = f"input/generation_{i}.png"
        r["bg_removal"] = {"by": "app", "model": "u2net", "rembg": sorted(opts), "work_size": it.get("work_size"),
                           "seconds": it.get("seconds")}
        if it.get("subject") is not None:
            r["subject"] = it["subject"]
        new = [c for c in it.get("warnings") or [] if c in IMAGE_NOTES and c not in r["warnings"]]
        for code in new:
            ctx.warn(tr(f"{i} 枚目の画像: {image_note(code)}", f"Image {i}: {image_note(code)}"), code=code)
        r["warnings"] = list(r["warnings"]) + new


def _images_log(image_meta: list[dict]) -> str:
    """入力画像の形式・画素数・透明の有無（ログ用。ファイル名・名前は出さない）。例「PNG 512×512 透明あり / JPEG …」。"""
    out = []
    for m in image_meta:
        fmt = f"{m['meta']['format']} {m['meta']['size'][0]}×{m['meta']['size'][1]}"
        out.append(tr(f"{fmt} 透明あり", f"{fmt} with transparency") if m["meta"]["has_alpha"]
                   else tr(f"{fmt} 透明なし", f"{fmt} no transparency"))
    return " / ".join(out)


def advanced_note(gen: dict) -> str:
    """既定から変えた生成の詳細設定（ログ用。SF05・SF15・SF17・SF20。既定のときは空で、ログは今までと同じ）。"""
    parts = []
    if gen.get("sampler_preset", "standard") != "standard":
        parts.append(tr(f"サンプリング {gen['sampler_preset']}", f"sampling {gen['sampler_preset']}"))
    if gen.get("low_vram"):
        parts.append(tr("低 VRAM モード", "low VRAM mode"))
    if gen.get("strict_offline"):
        parts.append(tr("通信の遮断", "network access blocked"))
    if gen.get("bg_app_removal"):
        opts = tr("・", ", ").join(k[len("bg_"):] for k in BG_OPTION_KEYS if gen.get(k))
        parts.append(tr(f"背景除去をアプリ側で（{opts}）", f"background removal in the app ({opts})") if opts
                     else tr("背景除去をアプリ側で", "background removal in the app"))
    if not parts:
        return ""
    joined = tr("・", " / ").join(parts)
    return tr(f"、詳細設定: {joined}", f", advanced settings: {joined}")


def _drop_samples(draft) -> None:
    """3D 生成が失敗した下書きの、候補の生成結果（「候補を保存する」。1 体約 45 MB）を消す。失敗した生成からは候補を選べない
    （candidates.view は生成が終わった下書きだけ）ので、失敗の記録として残す下書き（点検 C104）に容量を残さない（SF14）。"""
    from .providers.trellis import SAMPLE_DIR
    shutil.rmtree(os.path.join(draft.generate_dir, SAMPLE_DIR), ignore_errors=True)


def _generate(svc, ctx, prov, s, kind, gen, text, draft) -> str:
    # 生成に渡す画像: 背景除去の強制（SF19）・アプリ側の背景除去（SF20）では generation_file、ほかは入力のコピー
    imgs = [draft.path(*(r.get("generation_file") or r["file"]).split("/"))
            for r in (draft.data["input"].get("images") or [])] if kind == "image" else []
    # 見た目の作り直し（SF10。variant.run_variant）: 下書きに写した元の GLB（ほかの入力では None。要求は今までと同じ）
    vb = (draft.data["input"].get("variant") or {}).get("file") if kind == "text" else None
    req = GenerationRequest(kind=kind, settings=gen, text=text, image_path=imgs[0] if imgs else None,
                            image_paths=imgs or None,
                            image_labels=[r["label"] for r in draft.data["input"].get("images") or []] or None,
                            variant_base=draft.path(*vb.split("/")) if vb else None)
    run = prov.prepare(s, req, draft.generate_dir)
    # 生成 AI に渡した画像（下書きの中。候補を選んで生成し直すときに同じものを渡す。SF06・candidates.run_rebuild）
    draft.data["generation_inputs"] = [os.path.relpath(p, draft.dir).replace("\\", "/") for p in req.images]
    lo, hi = GEN_RANGE
    ctx.progress(lo + 0.01, "generate_load")

    def on_line(line: str) -> None:
        u = prov.parse_line(line, run)
        if u is None:
            return
        if u.progress is not None or u.stage:
            ctx.progress(None if u.progress is None else lo + (hi - lo) * u.progress, u.stage, u.detail)
        if u.log:
            ctx.info(u.log)

    multi = (tr(f" {len(imgs)} 枚・{gen['multi_mode']}", f": {len(imgs)} images, multi-image mode {gen['multi_mode']}")
             if len(imgs) > 1 else "")
    cands = gen["candidates"] if gen["select_pose"] else 1
    adv = advanced_note(gen)
    ctx.info(tr(f"{prov.display_name} で生成を開始（{kind}{multi}、シード {gen['seed']}、候補 {cands}、"
                f"simplify {gen['simplify']:g}、テクスチャ {gen['texture_size']}{adv}）",
                f"Starting generation with {prov.display_name} ({kind}{multi}, seed {gen['seed']}, candidates {cands}, "
                f"simplify {gen['simplify']:g}, texture {gen['texture_size']}{adv})"))
    rc, out = ctx.run_process(run.cmd, on_line, env=run.env, cwd=run.cwd)
    if rc != 0:
        _drop_samples(draft)
        code = prov.classify_failure(out, run)
        ep = run.state.get("error_params") or {}
        msg = (tr(f"{ep['index']} 枚目の画像に物体が見つかりませんでした", f"No object was found in image {ep['index']}")
               if ep.get("reason") == "image_no_object"
               else tr(f"{prov.display_name} の生成プロセスが終了コード {rc} で終わりました",
                       f"The {prov.display_name} generation process ended with exit code {rc}"))
        raise BackendError(code, msg, failure_text(out, 40), {"exit_code": rc, **ep})
    try:
        res = prov.collect(s, req, draft.generate_dir, run)
    except (FileNotFoundError, ValueError, KeyError) as e:
        _drop_samples(draft)
        raise BackendError("INVALID_MODEL", tr(f"生成結果の GLB がありません・読めません: {type(e).__name__}",
                                               f"The generated GLB is missing or cannot be read: {type(e).__name__}"),
                           failure_text(out, 20))
    rel = lambda p: os.path.relpath(p, draft.dir).replace("\\", "/")   # noqa: E731
    draft.data["model_id"] = res.model_id
    draft.data["seed"] = res.seed
    info = dict(res.info)
    pre = [rel(p) for p in info.pop("preprocessed", []) or []]      # 生成に使った画像（背景除去・切り抜きの後。画像ごと）
    # 注意は入力画像の注意（alpha_nearly_opaque など。_run_generate）＋ 生成 AI の注意
    gen_warnings = list(draft.data["input"].get("warnings") or []) + list(res.warnings)
    draft.data["generation"] = {"status": "done", "glb": rel(res.glb), "files": {k: rel(v) for k, v in res.files.items()},
                                "warnings": gen_warnings, "info": info, "axes": res.axes, "preprocessed": pre}
    draft.data["status"] = "converting"
    svc.drafts.save(draft)
    for w in res.warnings:
        ctx.warn(tr(f"生成の注意: {w}", f"Generation warning: {w}"))
    # 候補の色の付き方と、今の体がほぼ無彩色のときの注意（B-040。注意は記録せず、下書きの要約が今の体で決める）
    from . import candidate_colors, candidates
    cv = candidates.view(draft)
    line = candidate_colors.describe(cv)
    if line:
        ctx.info(line)
    if cv and cv.get("color_notice"):
        ctx.warn(tr(f"生成の注意: {cv['color_notice']}", f"Generation warning: {cv['color_notice']}"))
    ctx.progress(hi, "generate_done")
    return res.glb


def _convert(svc, ctx, draft, glb, gd, gen, prange, yaw_deg: int = 0) -> None:
    ctx.check_cancel()
    ctx.progress(prange[0], "convert_load")
    if yaw_deg:
        ctx.info(tr(f"変換の前にモデルを y 軸回りに {yaw_deg}° 回します（正面が逆向きの出力の修正）",
                    f"Rotating the model {yaw_deg}° around the y axis before conversion (fixes output that faces "
                    "backwards)"))
    tex_max = int(gen.get("convert_texture_max") or 2048)
    if tex_max != 2048:
        ctx.info(tr(f"テクスチャの最大辺を {tex_max} にして変換します（詳細設定）",
                    f"Converting with a maximum texture size of {tex_max} (advanced settings)"))
    params = {"glb": glb, "out_dir": draft.convert_dir, "name": "model", "refs_dir": svc.refs_dir(), "gd": gd,
              "target_vertices": gen["target_vertices"], "texture_max_size": tex_max,     # SF24（既定 2048 = 今までと同じ）
              "axes": conversion_axes((draft.data.get("generation") or {}).get("axes"), yaw_deg),
              "seed": draft.data.get("seed"), "model_id": draft.data.get("model_id"),
              "prompt": (draft.data.get("input") or {}).get("text")}
    res = ctx.run_worker("convert", params, prange, default_error="CONVERSION_FAILED")
    files = {k: "convert/" + v for k, v in (res.get("files") or {}).items()}
    flags = res.get("quality_flags") or {}
    load_flags = list(res.get("load_flags") or [])        # GLB の読み込みの注意（load_*。画面の文は Quality_load_*）
    draft.data["conversion"] = {"status": "converted", "dir": "convert", "name": res.get("name", "model"), "files": files,
                                "quality_flags": flags, "load_flags": load_flags,
                                "vertices": res.get("vertices"), "triangles": res.get("triangles"),
                                "warnings": sorted({f for v in flags.values() for f in v} | set(load_flags)),
                                "seconds": res.get("seconds"), "yaw_deg": yaw_deg}
    svc.drafts.save(draft)
    for g, fl in flags.items():
        if fl:
            ctx.warn(tr(f"変換の注意（男性）: {', '.join(fl)}", f"Conversion warnings (male): {', '.join(fl)}") if g == "m"
                     else tr(f"変換の注意（女性）: {', '.join(fl)}", f"Conversion warnings (female): {', '.join(fl)}"),
                     code=None)


def run_reconvert(svc, ctx, params: dict) -> dict:
    """下書きの GLB を変換し直す（3D 生成はやり直さない）。設定（目標頂点数）と向き（``yaw_deg``: 0 / 180）を変えられる。

    ``yaw_deg`` を省くと今の変換と同じ向き。失敗・取消のときは変換の結果が残らないので、下書きを ``failed`` にする
    （変換の記録の ``yaw_deg`` は前のまま。画面はもう一度同じ向きで変換し直せる）。"""
    draft = svc.drafts.get(params.get("draft_id", ""))
    s = svc.settings.get()
    gen = validate_generation({**(draft.data.get("settings") or s["generation"]), **(params.get("settings") or {})})
    conv = draft.data.get("conversion") or {}
    yaw = validate_yaw(params["yaw_deg"] if params.get("yaw_deg") is not None else conv.get("yaw_deg") or 0)
    gd, _w = preflight(svc, "glb", gen, ctx)
    glb_rel = (draft.data.get("generation") or {}).get("glb")
    if not glb_rel:
        raise BackendError("INVALID_MODEL", tr("この下書きには生成結果の GLB がありません", "This draft has no generated GLB"))
    draft.data["settings"] = gen
    draft.data["status"] = "converting"
    svc.drafts.save(draft)
    try:
        ctx.info(tr(f"下書き {draft.id} を変換し直します（向き {conv.get('yaw_deg') or 0}° → {yaw}°、目標頂点数 {gen['target_vertices']}）",
                    f"Reconverting draft {draft.id} (direction {conv.get('yaw_deg') or 0}° → {yaw}°, target vertex count "
                    f"{gen['target_vertices']})"))
        _convert(svc, ctx, draft, draft.path(glb_rel.replace("/", os.sep)), gd, gen, (0.02, 1.0), yaw_deg=yaw)
        draft.data["status"] = "ready"
        svc.drafts.save(draft)
    except Exception as e:      # noqa: BLE001  変換の失敗・取消・想定外の例外（OSError など）: failed（点検 R10A-01。_run_generate の C104 と同じ）
        err = e
        if not isinstance(e, (BackendError, JobCancelled)):
            err = BackendError("INTERNAL", f"{type(e).__name__}: {e}", failure_text(traceback.format_exc(), 30))
        draft.data["status"] = "failed"
        if draft.data.get("conversion"):
            draft.data["conversion"]["status"] = "failed"
        code, msg = ((err.code, err.message) if isinstance(err, BackendError)
                     else ("CANCELLED", tr("変換し直すのを取り消しました", "The reconversion was cancelled")))
        svc.drafts.add_error(draft, ctx.job.stage or "", code, msg)
        if isinstance(err, BackendError):
            err.params = {**err.params, "draft_id": draft.id}
        try:
            svc.drafts.save(draft)
        except (OSError, ValueError):    # 保存の失敗で元のエラーを隠さない（draft.json が converting のまま残れば、起動時の recover_interrupted が直す）
            pass
        if err is e:
            raise
        raise err from e
    return {"draft": draft.summary()}


# ---------------------------------------------------------------------------
# 背景除去の確認（D-020）
# ---------------------------------------------------------------------------
BG_PREVIEW_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bg_preview.py")
BG_PREVIEW_KEEP = 10


def summarize_bg_preview(params: dict) -> dict:
    return {"kind": "image"}


def run_bg_preview(svc, ctx, params: dict) -> dict:
    """生成の前に、TRELLIS に渡す画像（背景を除いて切り抜いた 518×518）を作って見せる。

    TRELLIS 実行環境の Python で ``bg_preview.py`` を動かす（rembg と同じ環境。TRELLIS 本体は読み込まないので数秒）。
    透明部分のある画像は背景除去をせず、切り抜きだけを行う（TRELLIS と同じ）。``bg_preview.py`` には生成と同じ
    copy_image で作った PNG を渡す（向き・16 bit・色のプロファイル・縮小の扱いが生成と食い違わないように。点検 C54）。
    結果の ``warnings`` は入力画像の注意（inspect_image・copy_image）と被写体の検査（SF21）、``input_coverage`` は元の
    画像の不透明な割合。

    ``force_bg_removal``（SF19）は生成と同じく透明部分を白に合成した RGB にしてから背景を除く。``settings`` は生成の設定
    （画面の今の値。省略した項目は保存してある設定）で、アプリ側の背景除去（SF20 の bg_app_removal）のときだけ rembg の
    後処理（bg_post_process_mask など）を生成と同じように使う。オフでは TRELLIS の中の処理と同じ（後処理なし）。"""
    from .providers.trellis import find_u2net
    # 初回セットアップ（列 setup）はパッケージの入れ替え・仮想環境の作り直しをするので、重なると rembg の読み込みや
    # セットアップのどちらかが失敗しうる（点検 C42。逆向きは setup.run_install が止める）
    busy = _setup_busy(svc, tr("背景除去を確認できません", "Background removal cannot be checked"))
    if busy:
        raise busy
    s = svc.settings.get()
    gen = validate_generation({**s["generation"], **(params.get("settings") or {})})
    force = params.get("force_bg_removal") is True
    root = resolve_trellis_root(s)
    path = params.get("image_path")
    secrets = [x for x in (path, os.path.basename(path) if isinstance(path, str) else None) if isinstance(x, str) and x]
    for x in secrets:
        redact.add_secret(x)
    try:
        meta = inspect_image(path)
        if root is None:
            raise BackendError("LIBRARY_MISSING", tr("TRELLIS 実行環境が見つかりません（背景除去は TRELLIS 実行環境の rembg を使う）",
                                                     "The TRELLIS runtime was not found (background removal uses rembg in "
                                                     "the TRELLIS runtime)"))
        f = find_u2net(root)
        if (not meta["has_alpha"] or force) and f is None:
            raise background_model_missing(root, not meta["has_alpha"])
        out_dir = os.path.join(svc.home(), "work", "_preview", ctx.job.id)
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.dirname(out_dir)
        # 古い確認結果は消す（今のジョブ ＋ 新しい順に KEEP - 1 個を残す）。今のジョブのフォルダーは必ず残す: ジョブ ID は
        # ローカル時刻なので、時計を戻した後は名前の順で今のジョブが先頭になり、自分の結果を消していた（点検 R10B-02）
        olds = sorted(n for n in os.listdir(base) if n != ctx.job.id)
        for old in olds[:max(0, len(olds) - (BG_PREVIEW_KEEP - 1))]:
            shutil.rmtree(os.path.join(base, old), ignore_errors=True)
        py = os.path.join(root, "venv", "Scripts", "python.exe")
        env = {"U2NET_HOME": f["home"] if f else os.path.join(root, "u2net")}
        ctx.progress(0.05, "bg_preview")
        app = gen["bg_app_removal"] and (not meta["has_alpha"] or force)
        opts = {k[len("bg_"):]: True for k in BG_OPTION_KEYS if gen.get(k)} if app else {}
        extra = tr("、背景除去を強制", ", forced background removal") if force and meta["has_alpha"] else ""
        if opts:
            names = tr("・", " / ").join(sorted(opts))
            extra += tr(f"、後処理: {names}", f", post-processing: {names}")
        fmt = f"{meta['format']} {meta['size'][0]}×{meta['size'][1]}"
        ctx.info(tr(f"背景除去の確認（画像 {fmt}、透明あり{extra}）",
                    f"Checking background removal (image {fmt}, with transparency{extra})") if meta["has_alpha"]
                 else tr(f"背景除去の確認（画像 {fmt}、透明なし{extra}）",
                         f"Checking background removal (image {fmt}, no transparency{extra})"))
        work = os.path.join(out_dir, "input.png")
        # 背景除去を強制した画像は alpha を使わないので、alpha から決まる注意を外す（生成の _run_generate と同じ）
        notes = [c for c in meta.get("warnings") or [] if not (force and c in ALPHA_NOTES)]
        notes += copy_image(path, work, flatten_alpha=force)
        res = ctx.run_worker("preview", {"image": work, "out": os.path.join(out_dir, "preprocessed.png"),
                                         "u2net": f["path"] if f else None, "u2net_md5_ok": f["md5_ok"] if f else None,
                                         "rembg": opts},
                             (0.05, 1.0), default_error="GENERATION_FAILED", python=py, script=BG_PREVIEW_SCRIPT, env=env)
        notes += [c for c in res.get("warnings") or [] if c in IMAGE_NOTES and c not in notes]     # 被写体の検査（SF21）
        return {**res, "has_alpha": meta["has_alpha"], "size": meta["size"], "input_coverage": meta["coverage"],
                "warnings": notes, "force_bg_removal": force, "app_removal": bool(app)}
    finally:
        for x in secrets:
            redact.remove_secret(x)
