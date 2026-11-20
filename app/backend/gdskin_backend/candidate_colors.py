"""候補の体の色の付き方（B-040、2026-11-05。決定は docs の D-072）。

テキスト→3D（TRELLIS-text-xlarge）は、文に色を書かないと白〜灰色の体になりやすい。候補の自動の選別（D-012・D-013）は
腕の角度と武器の疑いだけで選ぶので、ほかの候補に色の付いた体があっても、ほぼ無彩色の体を選ぶことがある。そこで、
生成スクリプトが書く候補の図（``<名前>_candidates.png``）から候補ごとの色の付き方を測り、

* 候補の一覧（candidates.view）の各候補に ``color``（``colored`` 色あり／``colorless`` ほぼ無彩色／``None`` 判定なし）を付け、
* 今の体がほぼ無彩色のとき、下書きの要約の注意（drafts.Draft.summary の ``warnings``）に次の 1 つを足す（記録はしない。
  要約のたびに今の体で決めるので、候補を選び直すと変わる）:

  - ``colorless_body``: 自動の選別で除外されていない、空でない、色の付いた候補がほかにある（「候補」のタブで選び直せる）
  - ``colorless_text``: そのような候補が無く、入力が文章（色を英語で書いて生成し直すよう案内する）。画像の入力では出さない
    （色は画像で決まる）

選別の基準（D-012）は変えない。候補の図は generate_glb.py の select_by_pose が作る: 1 体 320×320 の正面図
（pose_check.front_preview。メッシュの頂点の色をそのまま塗る。陰影なし。背景は 0.9 の灰色）を、候補の記録
（``<名前>_selection.json`` の candidates）の順に横に並べ、左上の 32 px までに文字（シード・腕の角度・CHOSEN・除外の理由）を
書く。空の候補は 230 の灰色の無地。大きさが「幅 320 × 候補の数、高さ 320」でない図（候補の選別なしの生成には図が無い）と
読めない図は測らない（印も注意も出さない）。

しきい値は、2026-11-05 に開発用の PC の実際の候補の図 23 枚（86 体。テキスト 22 枚・画像 1 枚。1 枚は別の図の写し）で
決めた（記録は docs/backlog.md の B-040）: ほぼ無彩色と判定した 30 体は彩度の平均が 0.005〜0.053、色ありの 56 体は 0.079 以上
（0.09 未満は色の濃い画素の割合で判定した 1 体）、判定なしは 0 体。11 枚 44 体は目視の分類と一致した。
2026-11-18 に彩度の平均で暗い画素を抑えた（MEAN_S_FLOOR_V。点検 BC-01）後の同じ 86 体: ほぼ無彩色 30 体（0.004〜0.045）、
色あり 54 体（0.09 以上は 38 体、ほかの 16 体は色の濃い画素の割合で判定）、判定なし 2 体（ほぼ黒の鎧に濃い赤が少しの体と、
暗い赤茶の体。抑えの前は色あり）。しきい値は変えていない。
"""
from __future__ import annotations

import os
import threading

TILE = 320                  # 1 体の正面図の大きさ（generate_glb.py の select_by_pose・pose_check.front_preview）
LABEL_BAND = 32             # 上の文字の帯（測らない）。この 2 つと生成スクリプトの描き方の一致は test_candidate_colors が確かめる
BG_TOL = 6                  # 背景（四隅の色の中央値）とこの階調以内の画素は体に数えない
MIN_PIXELS = 400            # 体の画素がこれより少ない候補は判定しない
STRONG_S, STRONG_V = 0.30, 0.20             # 色の濃い画素（彩度 > 0.30 かつ明度 > 0.20）
# 彩度の平均で、明度がこれより低い画素は (最大 − 最小) / MEAN_S_FLOOR_V で数える（暗い画素の彩度を抑える）。HSV の彩度は
# 暗い画素では 1〜3 階調の色みでも大きくなり、ほぼ黒の体が「色あり」になっていた（2026-11-18 点検 BC-01）。明るい画素は変わらない
MEAN_S_FLOOR_V = 0.5
COLORLESS_MEAN_S, COLORLESS_STRONG = 0.06, 0.02
COLORED_MEAN_S, COLORED_STRONG = 0.09, 0.05
NOTICE_ALTERNATIVE = "colorless_body"
NOTICE_TEXT = "colorless_text"
NOTICES = (NOTICE_ALTERNATIVE, NOTICE_TEXT)

_CACHE: dict = {}
_CACHE_MAX = 64
_lock = threading.Lock()


def tile_stats(tile) -> dict:
    """1 体の正面図（RGB の配列。上の文字の帯を除いたもの）→ {pixels: 体の画素の数, mean_s: 彩度の平均（暗い画素は
    MEAN_S_FLOOR_V で抑える）, strong: 色の濃い画素の割合（HSV の彩度）}。体の画素が無ければ mean_s・strong は None。"""
    import numpy as np
    t = np.asarray(tile)[..., :3].astype(np.int16)
    corners = np.array([t[0, 0], t[0, -1], t[-1, 0], t[-1, -1]])
    bg = np.median(corners, axis=0)
    body = np.abs(t - bg).max(axis=2) > BG_TOL
    n = int(body.sum())
    if n == 0:
        return {"pixels": 0, "mean_s": None, "strong": None}
    rgb = t[body].astype(np.float64) / 255.0
    mx, mn = rgb.max(axis=1), rgb.min(axis=1)
    s = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    strong = float(((s > STRONG_S) & (mx > STRONG_V)).mean())
    mean_s = float(((mx - mn) / np.maximum(mx, MEAN_S_FLOOR_V)).mean())
    return {"pixels": n, "mean_s": round(mean_s, 4), "strong": round(strong, 4)}


def classify(st: dict | None) -> str | None:
    """tile_stats の結果 → ``colorless``・``colored``・None（体が小さい・どちらとも言えない）。"""
    if not st or (st.get("pixels") or 0) < MIN_PIXELS or st.get("mean_s") is None:
        return None
    if st["mean_s"] < COLORLESS_MEAN_S and st["strong"] < COLORLESS_STRONG:
        return "colorless"
    if st["mean_s"] >= COLORED_MEAN_S or st["strong"] >= COLORED_STRONG:
        return "colored"
    return None


def measure_sheet(path: str, count: int) -> list[dict] | None:
    """候補の図 → 候補ごとの {pixels, mean_s, strong, color}（図の左から）。大きさが違う・読めない図は None。
    同じファイル（パス・更新日時・大きさ）は測り直さない（下書きの要約は一覧・ジョブの結果のたびに作るため）。"""
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    key = (os.path.normcase(os.path.abspath(path)), st.st_mtime_ns, st.st_size, count)
    with _lock:
        if key in _CACHE:
            return _CACHE[key]
    res = _measure(path, count)
    with _lock:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = res
    return res


def _measure(path: str, count: int) -> list[dict] | None:
    import numpy as np
    from PIL import Image
    # 下書きの要約（一覧・ジョブの結果）を止めないよう、壊れた図（PNG の途中の破損では OSError のほかに SyntaxError・
    # zlib.error も出うる）は理由を問わず「測らない」にする
    try:
        with Image.open(path) as im:
            if im.size != (TILE * count, TILE):          # 図を開いただけでは画素を読まない（大きさを先に確かめる）
                return None
            arr = np.asarray(im.convert("RGB"))
        out = []
        for i in range(count):
            s = tile_stats(arr[LABEL_BAND:, i * TILE:(i + 1) * TILE])
            out.append({**s, "color": classify(s)})
        return out
    except Exception:       # noqa: BLE001
        return None


def for_draft(draft, sel: dict) -> dict[int, str | None]:
    """下書きの候補の図と候補の記録 → {シード: 色の判定}。図・記録が無い、図の大きさが記録の数と合わないときは {}。"""
    gen = draft.data.get("generation")
    files = gen.get("files") if isinstance(gen, dict) else None
    rel = files.get("candidates_png") if isinstance(files, dict) else None
    raw = sel.get("candidates") if isinstance(sel, dict) else None
    if not isinstance(rel, str) or not rel or not isinstance(raw, list) or not raw:
        return {}
    res = measure_sheet(draft.path(*rel.split("/")), len(raw))
    if res is None:
        return {}
    return {c["seed"]: m["color"] for c, m in zip(raw, res)
            if isinstance(c, dict) and isinstance(c.get("seed"), int) and not isinstance(c.get("seed"), bool)}


def describe(view: dict | None) -> str | None:
    """候補の一覧（candidates.view）→ 生成のログの 1 行（色を判定した候補が無ければ None）。"""
    from .i18n import tr
    items = (view or {}).get("items") or []
    if not any(i.get("color") for i in items):
        return None
    parts = []
    for i in items:
        label = {"colored": tr("色あり", "has color"),
                 "colorless": tr("ほぼ無彩色", "almost no color")}.get(i.get("color")) or tr("判定なし", "not judged")
        mark = tr("（今の体）", " (current body)") if i.get("current") else ""
        parts.append(tr(f"シード {i.get('seed')} {label}{mark}", f"seed {i.get('seed')} {label}{mark}"))
    joined = tr("、", ", ").join(parts)
    return tr(f"候補の色（候補の図から）: {joined}", f"Candidate colors (from the candidate image): {joined}")


def notice(items: list[dict], current_seed, kind) -> str | None:
    """候補の一覧の行（candidates.view の items。color 付き）と今の体のシード → 注意（NOTICES の 1 つ）か None。"""
    cur = next((i for i in items if i.get("seed") == current_seed), None)
    if cur is None or cur.get("color") != "colorless":
        return None
    if any(i.get("seed") != current_seed and i.get("color") == "colored" and i.get("selectable") and not i.get("excluded")
           for i in items):
        return NOTICE_ALTERNATIVE
    return NOTICE_TEXT if kind == "text" else None
