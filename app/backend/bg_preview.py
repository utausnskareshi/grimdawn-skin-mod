"""背景除去の確認（D-020）: ``python -I -X utf8 bg_preview.py preview <引数の JSON>``。

TRELLIS 実行環境の Python で動かす（rembg・onnxruntime・Pillow・numpy はその環境のもの）。TRELLIS 本体（torch）は読み込まない。
処理は TRELLIS の ``TrellisImageTo3DPipeline.preprocess_image``（trellis/pipelines/trellis_image_to_3d.py、MIT License,
Copyright (c) Microsoft Corporation）と同じ: 透明部分のある画像はそのまま、無い画像は 1024 以下に縮めて rembg（u2net）で
背景を除く → 不透明度 0.8 を超える範囲を 1.2 倍に広げた正方形で切り抜く → 518×518。確認用に、TRELLIS が最後に行う
「黒背景への合成」の前の RGBA を保存する。

引数: {"image": 入力（バックエンドが生成と同じ pipeline.copy_image で作った PNG。点検 C54）, "out": 出力の PNG,
       "u2net": モデルのパス（透明部分のある画像では null でよい）, "rembg": rembg の後処理（下の remove と同じ。省略可）}
環境変数 U2NET_HOME はバックエンドがモデルの場所に向けて渡す。モデルが無いときは rembg を呼ばずに止める
（rembg はモデルが無いと GitHub から取得しようとするため）。
標準出力に ``@@GDSKIN {...}`` の行（progress / log / result / error。docs/app_architecture.md 3.3 節）。

``python -I -X utf8 bg_preview.py remove <引数の JSON>``（2026-10-17 追加機能 SF20。詳細設定の「アプリ側で背景を除く」）:
生成の前に、TRELLIS の preprocess_image の前半（長辺 1,024 以下に縮めて rembg u2net で背景を除く）と同じ処理で背景を
除いた RGBA（切り抜きの前）を書く。TRELLIS は透明部分のある画像の背景を除かずに切り抜きだけを行うので、生成に使う画像は
「背景除去を確認」（preview に同じ "rembg" を渡したもの）と同じになる。
引数: {"images": [{"index": 番号, "image": 入力の PNG, "out": 出力の PNG}], "u2net": モデルのパス,
       "rembg": {"post_process_mask": bool, "alpha_matting": bool, "decontaminate": bool}}
rembg の session は u2net を明示する（remove の既定は非商用の bria-rmbg。D-020）。vitmatte はモデルを固定せずに取得する
ので使わない（常に False）。

被写体の検査（2026-10-17 追加機能 SF21。警告だけ）: preview・remove の結果の ``subject``（subject_checks）と ``warnings``。
バックエンドの pipeline.inspect_image も透明部分のある画像に同じ関数を使う（このファイルは numpy と Pillow だけで動く）。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

TRELLIS_SIZE = 518
ALPHA_THRESHOLD = 0.8 * 255
REMBG_OPTIONS = ("post_process_mask", "alpha_matting", "decontaminate")

# 被写体の検査（SF21。止めない注意）。閾値は経験的な値で、誤った注意を減らす方に寄せている
SUBJECT_EDGE_FRACTION = 0.01   # 辺に接する不透明な画素が、その辺の長さのこの割合以上（かつ SUBJECT_EDGE_MIN_PX 以上）なら「接する」
SUBJECT_EDGE_MIN_PX = 2
SUBJECT_SMALL_UPSCALE = 2.0    # TRELLIS の切り抜き（範囲の 1.2 倍の正方形）を 518 px にする拡大がこれを超えると「小さい」
SUBJECT_GRID = 256             # 塊を数える格子の長辺（升はその範囲のどれかの画素が不透明なら不透明）
SUBJECT_OTHER_AREA = 0.02      # 最大の塊に対する面積の割合。これ以上のほかの塊があれば「複数の物体」
SUBJECT_SPREAD = 1.25          # 不透明な範囲の全体 ÷ 最大の塊の範囲（一辺）。離れた小さな破片が切り抜きを広げるとき
SUBJECT_CODES = ("subject_edge", "subject_small", "subject_multiple")


# 表示言語（日本語・英語）ごとの文の選択（docs/decisions.md D-060）。TRELLIS 実行環境の Python で -I で動き gdskin_backend を
# 読み込めないので、src/gdconvert/_i18n.py と同じ規則の関数をここに置く（GDSKIN_LANG はバックエンドから受け継ぐ）
def lang() -> str:
    """今の表示言語（"ja" / "en"）。"""
    s = (os.environ.get("GDSKIN_LANG") or "").strip().lower()
    return "en" if s == "en" or s.startswith("en-") else "ja"


def tr(ja: str, en: str) -> str:
    """表示言語の文（英語なら en、ほかは ja）。"""
    return en if lang() == "en" else ja


def emit(type_: str, **kw) -> None:
    sys.stdout.write("@@GDSKIN " + json.dumps({"type": type_, **kw}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class NoSubject(Exception):
    """背景を除いた結果、不透明な部分（キャラクター）が残らなかった。"""


class ModelMissing(Exception):
    """背景除去のモデル（u2net.onnx）が無い（rembg を呼ぶ前に確かめる）。rembg の中で起きた FileNotFoundError
    （numba のキャッシュの書き込みなど）とは分ける（点検 C48。以前はどちらも「モデルがありません」になっていた）。"""


class ModelUnreadable(Exception):
    """背景除去のモデル（u2net.onnx）を onnxruntime が読み込めない（途中で切れた・壊れたファイルの InvalidProtobuf など。
    onnxruntime の例外は OSError ではないので、以前は error 行が出ずに原因の分からない失敗になっていた。点検 R10A-02）。"""

    def __init__(self, model: str, error: BaseException):
        super().__init__(f"{model}: {type(error).__name__}: {error}")
        self.model = model
        self.error = error


# onnxruntime がモデルのファイルを読めないときの状態（壊れた・途中で切れたファイル）
_DAMAGED_STATUS = ("INVALID_PROTOBUF", "NO_SUCHFILE")


def model_damaged(e: ModelUnreadable, md5_ok) -> bool:
    """読み込めないのがファイルの破損によるものか: 照合値が rembg の記録と違う（md5_ok が False）か、onnxruntime が
    ファイルの形式の誤りを返した。照合値が合っていて別の理由（onnxruntime の版など）なら False。"""
    return md5_ok is False or any(s in str(e.error) for s in _DAMAGED_STATUS)


def has_transparency(image) -> bool:
    import numpy as np
    if image.mode != "RGBA":
        return False
    return not np.all(np.array(image)[:, :, 3] == 255)


def cutout(image, remove):
    """TRELLIS の preprocess_image の前半 → (RGBA, 背景を除いたか)。透明部分のある画像はそのまま。無い画像は長辺 1,024 以下に
    縮めてから remove(rgb_image) -> rgba_image（rembg.remove）で背景を除く（結果は縮めた後の大きさ）。"""
    from PIL import Image
    if has_transparency(image):
        return image, False
    rgb = image.convert("RGB")
    scale = min(1, 1024 / max(rgb.size))
    if scale < 1:
        rgb = rgb.resize((int(rgb.width * scale), int(rgb.height * scale)), Image.Resampling.LANCZOS)
    return remove(rgb).convert("RGBA"), True


def mask_from_alpha(alpha):
    """alpha（L の画像）→ TRELLIS が切り抜きに使う不透明な画素（alpha > 0.8×255）を 255、ほかを 0 にした L の画像。"""
    return alpha.point(lambda v: 255 if v > ALPHA_THRESHOLD else 0)


def opaque_mask(rgba):
    return mask_from_alpha(rgba.getchannel("A"))


def _components(grid) -> list[tuple[int, int, int, int, int]]:
    """格子（bool の 2 次元配列）の 8 近傍でつながる塊 → [(面積, x0, y0, x1, y1)]（升の単位。x1・y1 は含む）。
    行ごとの連続した範囲（ラン）を前の行のランとつなぐ（素集合）。scipy を使わない（バックエンドの環境には無い）。"""
    import numpy as np
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    runs, prev = [], []
    for y in range(grid.shape[0]):
        d = np.diff(np.concatenate(([0], grid[y].astype(np.int8), [0])))
        cur, j = [], 0
        for s, e in zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()):   # [s, e)
            lab = len(parent)
            parent.append(lab)
            while j < len(prev) and prev[j][1] < s:      # 前の行のラン [ps, pe) は pe >= s かつ ps <= e なら斜めでも接する
                j += 1
            k = j
            while k < len(prev) and prev[k][0] <= e:
                ra, rb = find(lab), find(prev[k][2])
                if ra != rb:
                    parent[ra] = rb
                k += 1
            cur.append((s, e, lab))
            runs.append((lab, y, s, e))
        prev = cur
    comps: dict[int, list[int]] = {}
    for lab, y, s, e in runs:
        c = comps.setdefault(find(lab), [0, s, y, e - 1, y])
        c[0] += e - s
        c[1], c[2], c[3], c[4] = min(c[1], s), min(c[2], y), max(c[3], e - 1), max(c[4], y)
    return [tuple(c) for c in comps.values()]


def subject_checks(mask, removed: bool) -> dict:
    """被写体の検査（2026-10-17 追加機能 SF21。止めない注意）→ {"warnings": [コード], 詳細}。

    mask は mask_from_alpha の結果で、TRELLIS が切り抜く画像と同じ大きさ（背景を除く画像は長辺 1,024 以下に縮めた後、
    透明部分のある画像はそのまま）。removed は背景を除いた結果か（False は入力の透明部分をそのまま使う画像）。

    * ``subject_edge``: 被写体が画像の端に接している（足・頭などが切れている恐れ）。透明部分のある画像で、4 辺すべてに
      接するもの（被写体の範囲で切り詰めて保存した PNG）は切れているとはみなさない（``trimmed``）。
    * ``subject_small``: TRELLIS の切り抜き（範囲の 1.2 倍の正方形）が小さく、518 px への拡大が SUBJECT_SMALL_UPSCALE を超える。
    * ``subject_multiple``: 離れた不透明な塊が複数ある（ほかの塊の面積が最大の塊の SUBJECT_OTHER_AREA 以上、または
      離れた破片で不透明な範囲が最大の塊の SUBJECT_SPREAD 倍以上に広がる。TRELLIS は不透明な画素をすべて囲んで切り抜く）。"""
    import numpy as np
    w, h = mask.size
    info: dict = {"warnings": [], "size": [w, h]}
    box = mask.getbbox()
    if box is None:
        return info
    x0, y0, x1, y1 = box[0], box[1], box[2] - 1, box[3] - 1
    crop = int(max(x1 - x0, y1 - y0) * 1.2)          # TRELLIS の切り抜きの一辺（preprocess_image と同じ計算）
    upscale = TRELLIS_SIZE / max(crop, 1)
    edges = []
    for side, b in (("top", (0, 0, w, 1)), ("bottom", (0, h - 1, w, h)), ("left", (0, 0, 1, h)), ("right", (w - 1, 0, w, h))):
        length = w if side in ("top", "bottom") else h
        if mask.crop(b).histogram()[255] >= max(SUBJECT_EDGE_MIN_PX, SUBJECT_EDGE_FRACTION * length):
            edges.append(side)
    trimmed = not removed and box == (0, 0, w, h)
    k = max(1, math.ceil(max(w, h) / SUBJECT_GRID))
    grid = np.asarray(mask.reduce(k) if k > 1 else mask) > 0
    comps = sorted(_components(grid), reverse=True)
    objects, spread = 1, 1.0
    if comps:
        main = comps[0]
        objects = 1 + sum(1 for c in comps[1:] if c[0] >= SUBJECT_OTHER_AREA * main[0])
        ys, xs = np.flatnonzero(grid.any(axis=1)), np.flatnonzero(grid.any(axis=0))
        spread = (max(int(xs[-1] - xs[0]), int(ys[-1] - ys[0])) + 1) / (max(main[3] - main[1], main[4] - main[2]) + 1)
    if edges and not trimmed:
        info["warnings"].append("subject_edge")
    if upscale > SUBJECT_SMALL_UPSCALE:
        info["warnings"].append("subject_small")
    if objects > 1 or spread >= SUBJECT_SPREAD:
        info["warnings"].append("subject_multiple")
    info.update(bbox=[x0, y0, x1, y1], crop=crop, upscale=round(upscale, 3), edges=edges, trimmed=trimmed,
                objects=objects, components=len(comps), spread=round(spread, 3), cell_px=k)
    return info


def _source_bbox(bbox, work_size, source_size) -> list[int]:
    """縮めた後（work_size）の範囲 [x0, y0, x1, y1]（x1・y1 は含む）→ 元の画像（source_size）の座標。"""
    sx, sy = source_size[0] / work_size[0], source_size[1] / work_size[1]
    return [int(bbox[0] * sx), int(bbox[1] * sy),
            min(source_size[0] - 1, math.ceil((bbox[2] + 1) * sx) - 1), min(source_size[1] - 1, math.ceil((bbox[3] + 1) * sy) - 1)]


def preprocess(image, remove):
    """TRELLIS の preprocess_image と同じ切り抜き（黒背景への合成の前の RGBA）→ (画像, 情報)。

    remove(rgb_image) -> rgba_image は背景除去（rembg.remove）。透明部分のある画像では呼ばない。
    情報の ``bbox`` は入力の画像（``source_size``）の座標。背景を除いた画像では TRELLIS が縮めた後（``work_size``）に
    範囲を求めるので、その座標は ``work_bbox``（点検の指摘: 以前は縮めた後の座標を元の大きさと並べて返していた）。"""
    import numpy as np
    from PIL import Image
    output, removed = cutout(image, remove)
    alpha = np.array(output)[:, :, 3]
    pts = np.argwhere(alpha > ALPHA_THRESHOLD)
    if len(pts) == 0:
        raise NoSubject()
    bbox = int(pts[:, 1].min()), int(pts[:, 0].min()), int(pts[:, 1].max()), int(pts[:, 0].max())
    center = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    size = int(max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * 1.2)
    crop = (center[0] - size // 2, center[1] - size // 2, center[0] + size // 2, center[1] + size // 2)
    out = output.crop(crop).resize((TRELLIS_SIZE, TRELLIS_SIZE), Image.Resampling.LANCZOS)
    coverage = float((np.array(out)[:, :, 3] > ALPHA_THRESHOLD).mean())
    subject = subject_checks(opaque_mask(output), removed)
    return out, {"removed": removed, "bbox": _source_bbox(bbox, output.size, image.size), "work_bbox": list(bbox),
                 "work_size": [output.width, output.height], "coverage": round(coverage, 4),
                 "source_size": [image.width, image.height], "subject": subject, "warnings": list(subject["warnings"])}


def rembg_kwargs(opts) -> dict:
    """rembg.remove に足す後処理（SF20。指定が無ければ空で、呼び出しは今までと同じ）。vitmatte はモデルを固定せずに
    取得するので、後処理を使うときは明示的に False にする。"""
    kw = {k: True for k in REMBG_OPTIONS if isinstance(opts, dict) and opts.get(k) is True}
    if kw:
        kw["vitmatte"] = False
    return kw


def load_image(path: str):
    """入力の PNG → RGB / RGBA（向きの EXIF を反映）。入力はバックエンドの copy_image の PNG（RGB / RGBA。正規化は
    copy_image の 1 か所）なので実質何もしない（このスクリプトを直接呼ぶ場合のために残す）。
    中身の形式はバックエンドの入力と同じ 4 形式に限る（拡張子 .png の EPS で Ghostscript を起動しない。点検 R10B-04）。"""
    from PIL import Image, ImageOps
    with Image.open(path, formats=("PNG", "JPEG", "WEBP", "BMP")) as im:
        im = ImageOps.exif_transpose(im)
        mode = "RGBA" if (im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info) else "RGB"
        return im.convert(mode)


def main(argv: list[str]) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(argv) != 2 or argv[0] not in ("preview", "remove"):
        emit("error", code="INTERNAL", message=tr("使い方: bg_preview.py preview|remove <引数の JSON>",
                                                  "Usage: bg_preview.py preview|remove <arguments JSON>"))
        return 2
    with open(argv[1], encoding="utf-8") as f:
        params = json.load(f)
    t0 = time.time()
    stage = "bg_preview" if argv[0] == "preview" else "bg_remove"
    emit("progress", progress=0.1 if argv[0] == "preview" else 0.0, stage=stage)
    session = {}
    extra = rembg_kwargs(params.get("rembg"))

    def remove(rgb):
        model = params.get("u2net")
        if not model or not os.path.isfile(model):
            raise ModelMissing(model or "u2net.onnx")
        if argv[0] == "preview":
            emit("progress", progress=0.3, stage=stage)
        import rembg
        if "s" not in session:
            try:
                session["s"] = rembg.new_session("u2net")     # 明示する（remove の既定は非商用の bria-rmbg。D-020）
            except Exception as e:
                # onnxruntime のモデルの読み込みの失敗（型は onnxruntime の独自の例外で、共通の基底は Exception だけ）。
                # ほかの例外（rembg の中の OSError など）は今までどおり
                if type(e).__module__.startswith("onnxruntime"):
                    raise ModelUnreadable(model, e) from e
                raise
        if argv[0] == "preview":
            emit("progress", progress=0.6, stage=stage)
        return rembg.remove(rgb, session=session["s"], **extra)

    index = None
    try:
        if argv[0] == "remove":
            items = []
            todo = params.get("images") or []
            for n, it in enumerate(todo):
                index = it.get("index")
                t1 = time.time()
                output, removed = cutout(load_image(it["image"]), remove)
                mask = opaque_mask(output)
                if mask.getbbox() is None:
                    raise NoSubject()
                subject = subject_checks(mask, removed)
                os.makedirs(os.path.dirname(os.path.abspath(it["out"])), exist_ok=True)
                output.save(it["out"], "PNG")
                items.append({"index": index, "path": os.path.abspath(it["out"]), "removed": removed,
                              "work_size": [output.width, output.height], "subject": subject,
                              "warnings": list(subject["warnings"]), "seconds": round(time.time() - t1, 2)})
                emit("progress", progress=(n + 1) / len(todo), stage=stage)
            emit("result", data={"images": items, "rembg": sorted(k for k in extra if k != "vitmatte"),
                                 "seconds": round(time.time() - t0, 2)})
            return 0
        image = load_image(params["image"])
        out, info = preprocess(image, remove)
    except ModelMissing as e:
        emit("error", code="MODEL_MISSING", message=tr(f"背景除去のモデルがありません: {e}",
                                                       f"The background removal model is missing: {e}"),
             params={"reason": "u2net"})
        return 1
    except ModelUnreadable as e:
        if model_damaged(e, params.get("u2net_md5_ok")):
            # 壊れた・途中で切れたモデル: セットアップで取り直す・照合し直すよう案内する（画面は reason u2net_damaged の文）
            emit("error", code="MODEL_MISSING",
                 message=tr(f"背景除去のモデル（u2net.onnx）を読み込めません。ファイルが壊れている可能性があります: {e}",
                            f"Cannot load the background removal model (u2net.onnx). The file may be damaged: {e}"),
                 params={"reason": "u2net_damaged"})
        else:
            emit("error", code="GENERATION_FAILED", message=tr(f"背景除去のモデルを読み込めません: {e}",
                                                               f"Cannot load the background removal model: {e}"))
        return 1
    except NoSubject:
        emit("error", code="INVALID_INPUT",
             message=tr(f"背景を除いた結果、キャラクターが残りませんでした（{index} 枚目の画像）",
                        f"No character was left after removing the background (image {index})") if index
             else tr("背景を除いた結果、キャラクターが残りませんでした", "No character was left after removing the background"),
             params={"reason": "background_removal_empty", **({"index": index} if index else {})})
        return 1
    except ImportError as e:
        emit("error", code="LIBRARY_MISSING", message=tr(f"rembg を読み込めません: {e}", f"Cannot load rembg: {e}"))
        return 1
    except OSError as e:
        # rembg・onnxruntime・numba の中で起きたファイルの失敗（キャッシュの書き込みなど）。モデルの有無とは別の失敗
        emit("error", code="GENERATION_FAILED", message=tr(f"背景除去に失敗しました: {type(e).__name__}: {e}",
                                                           f"Background removal failed: {type(e).__name__}: {e}"))
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(params["out"])), exist_ok=True)
    out.save(params["out"], "PNG")
    emit("progress", progress=1.0, stage="bg_preview")
    emit("result", data={"path": os.path.abspath(params["out"]), **info, "rembg": sorted(k for k in extra if k != "vitmatte"),
                         "seconds": round(time.time() - t0, 2)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
