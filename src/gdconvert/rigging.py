"""RiggingProcessor: 基準ボディへの合わせ込み、ウェイト転写、バインド姿勢への戻し。

流れ（性別ごと）:

1. :func:`prealign` — 入力の高さを基準ボディの身長に、足元を y=0 に、胴の中心を基準ボディの中心に合わせる
2. :func:`fit_pose` — 基準ボディ（骨格付き）を入力の形に合わせて変形させる姿勢・比率を最小二乗で求める
   （腕の開き、肘・膝の曲げ、脚の開き、胴・腕・脚の長さ、肩幅、全体の拡大と向き）
3. :func:`transfer_weights` — 合わせた姿勢の基準ボディ表面の最近点からウェイトを補間する。
   遠い頂点・法線が合わない頂点は、確かな頂点のウェイトからラプラス方程式で補間（inpainting）し、
   軽く平滑化してから影響ボーンを最大 4 本に絞る
4. :func:`unpose` — 求めた姿勢の逆変換で入力をゲームのバインド姿勢（A ポーズ）に戻す
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from scipy import optimize, sparse
from scipy.sparse.linalg import splu
from scipy.spatial import cKDTree

from . import geometry
from ._i18n import tr
from .refs import SourceBody
from .skeleton import PoseModel, default_fit_spec, inverse_lbs, lbs

__all__ = ["Prealign", "prealign", "extreme_outliers", "core_height", "FitResult", "fit_pose", "identity_fit",
           "TransferResult", "transfer_weights", "unpose", "ground_snap", "dense_lbs"]


# ----------------------------------------------------------------------
# 事前の位置合わせ
# ----------------------------------------------------------------------
@dataclass
class Prealign:
    scale: float
    translation: np.ndarray          # 拡大の後に足す
    source_height: float
    body_height: float
    outlier_vertices: int = 0        # 高さ・中心の計算から除いた、大きく離れた頂点の数（:func:`_core_faces`）

    def apply(self, p: np.ndarray) -> np.ndarray:
        return np.asarray(p, dtype=np.float64) * self.scale + self.translation


# 外れ値の判定: 頂点の座標の 0.5〜99.5 パーセンタイルの範囲から、その範囲の高さの OUTLIER_MARGIN 倍より外にある頂点。
# 実サンプル 127 件ではみ出しは最大 0.13 倍（翼・槍・マント）なので、通常の入力では起きない
OUTLIER_MARGIN = 1.0
# 変換を止める極端な外れ値: 同じ範囲から、その範囲の高さの OUTLIER_STOP_MARGIN 倍より外にある、面が使う頂点
# （決定 #2 R03B-03、2026-11-04 利用者の回答「推奨どおり」）。身長の 100 倍も離れた頂点は壊れた・余計な部品とみなし、
# pipeline が工程 2 で INVALID_MODEL（gltf.GltfError の理由 outlier_vertices）で止める。境目ちょうどは止めない。
# OUTLIER_MARGIN 倍〜この倍率の頂点は今までどおり合わせ込みから除いて変換を続け、品質の注意 outlier_vertices を出す
OUTLIER_STOP_MARGIN = 100.0
# 止めたときの文と params の distance（範囲の高さの何倍か）の上限。これより遠い（無限大になるものを含む）頂点は、この値の
# 「〜倍以上」と示す（点検 D2R-04。1 頂点の x = 3e38 で 39 桁の数が画面に出ていた。int(inf) の OverflowError も防ぐ）
OUTLIER_DISTANCE_CAP = 1_000_000


def _outlier_range(P: np.ndarray):
    """外れ値の判定の範囲: 頂点の座標の 0.5・99.5 パーセンタイル (lo, hi) と範囲の高さ hr（y）。
    判定できない（hr が 0・非有限）ときは None。"""
    with np.errstate(invalid="ignore"):
        lo, hi = np.percentile(P, 0.5, axis=0), np.percentile(P, 99.5, axis=0)
        hr = float(hi[1] - lo[1])
    if not (np.isfinite(hr) and hr > 0):
        return None
    return lo, hi, hr


def extreme_outliers(P: np.ndarray, F: np.ndarray) -> tuple[int, float]:
    """面が使う頂点のうち、範囲から :data:`OUTLIER_STOP_MARGIN` 倍より外にある頂点の数と、その中で範囲から最も離れた
    頂点の距離（範囲の外までの軸ごとの距離の最大を、範囲の高さ hr の倍数で）。無い・判定できないときは (0, 0.0)。"""
    P = np.asarray(P, dtype=np.float64)
    rng = _outlier_range(P)
    if rng is None:
        return 0, 0.0
    lo, hi, hr = rng
    Pu = P[np.unique(np.asarray(F))]
    with np.errstate(invalid="ignore"):
        out = ~((Pu >= lo - OUTLIER_STOP_MARGIN * hr) & (Pu <= hi + OUTLIER_STOP_MARGIN * hr)).all(axis=1)
    n = int(out.sum())
    if not n:
        return 0, 0.0
    excess = np.maximum(lo - Pu[out], Pu[out] - hi).max(axis=1)
    with np.errstate(over="ignore"):        # 極端に遠い頂点の倍率は inf になりうる（呼ぶ側が OUTLIER_DISTANCE_CAP で切る）
        return n, float(excess.max() / hr)


def _core_faces(P: np.ndarray, F: np.ndarray):
    """大きく離れた頂点（:data:`OUTLIER_MARGIN`）を含まない面と、除いた頂点の数。外れた頂点が無い・判定できない
    （範囲の高さ 0、非有限の座標、残る面が無い）ときは None（従来どおり全体で測る）。"""
    rng = _outlier_range(P)
    if rng is None:
        return None
    lo, hi, hr = rng
    with np.errstate(invalid="ignore"):
        inside = ((P >= lo - OUTLIER_MARGIN * hr) & (P <= hi + OUTLIER_MARGIN * hr)).all(axis=1)
    F = np.asarray(F)
    used = np.unique(F)
    if inside[used].all():
        return None
    Fc = F[inside[F].all(axis=1)]
    if not len(Fc):
        return None
    return Fc, int((~inside[used]).sum())


def core_height(P: np.ndarray, F: np.ndarray) -> tuple[float, int] | None:
    """大きく離れた頂点を除いた高さ（:func:`prealign` と同じ規則: :func:`_core_faces` の面が使う頂点の y の範囲）と、
    除いた頂点の数。外れた頂点が無い・判定できない、または除いた残りの高さが 0（測れない）ときは None（呼ぶ側は従来どおり
    全体の高さで測る）。pipeline が溶接の許容の高さに使う（決定 D-068）。"""
    P = np.asarray(P, dtype=np.float64)
    core = _core_faces(P, F)
    if core is None:
        return None
    Fc, n_out = core
    Pc = P[np.unique(Fc)]
    h = float(Pc[:, 1].max() - Pc[:, 1].min())
    if not (np.isfinite(h) and h > 0):
        return None
    return h, n_out


def _band_center(P: np.ndarray, F: np.ndarray, lo: float, hi: float, rng=None) -> np.ndarray:
    pts, _, _ = geometry.sample_surface(P, F, 20000, rng=rng)
    y0, y1 = pts[:, 1].min(), pts[:, 1].max()
    h = y1 - y0
    sel = (pts[:, 1] >= y0 + lo * h) & (pts[:, 1] <= y0 + hi * h)
    q = pts[sel] if sel.sum() > 100 else pts
    return np.array([np.median(q[:, 0]), y0, np.median(q[:, 2])])


def prealign(P: np.ndarray, F: np.ndarray, body: SourceBody, scale: float = 0.0) -> Prealign:
    """入力の高さ（バウンディングボックス）→ 基準ボディの身長、最下点 → y=0、
    胴（高さ 45〜85%）の水平中心の中央値 → 基準ボディの同じ帯の中心。

    大きく離れた頂点（:func:`_core_faces`。壊れた・余計な部品を含む GLB の、体から身長の何倍も離れた頂点）があるときは、
    それを含む面を除いて高さ・最下点・中心を測る（1 個の頂点でモデル全体が極小になる・中心がずれるのを防ぐ）。"""
    P = np.asarray(P, dtype=np.float64)
    core = _core_faces(P, F)
    Fc, n_out = (F, 0) if core is None else core
    Pc = P if core is None else P[np.unique(Fc)]
    h = float(Pc[:, 1].max() - Pc[:, 1].min())
    if h <= 0:
        raise ValueError(tr("高さ 0 のモデル", "The model has zero height"))
    bh = float(body.positions[:, 1].max() - body.positions[:, 1].min())
    s = scale if scale > 0 else bh / h
    c_src = _band_center(P * s, Fc, 0.45, 0.85, np.random.default_rng(1))
    c_dst = _band_center(body.positions, body.faces, 0.45, 0.85, np.random.default_rng(2))
    t = np.array([c_dst[0] - c_src[0], float(body.positions[:, 1].min()) - c_src[1], c_dst[2] - c_src[2]])
    return Prealign(float(s), t, h, bh, n_out)


# ----------------------------------------------------------------------
# 姿勢の合わせ込み
# ----------------------------------------------------------------------
@dataclass
class FitResult:
    names: list[str]
    x: np.ndarray
    transforms: np.ndarray           # (nb, 4, 4) バインド空間 → 入力（事前合わせ後）の空間
    rms_before: float                # 基準ボディの点 → 入力表面 の距離の RMS（バインド姿勢のまま）
    rms_after: float
    model_to_body_before: float      # 入力の点 → 基準ボディ の距離の中央値
    model_to_body_after: float
    nfev: int = 0
    seconds: float = 0.0
    message: str = ""
    stages: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        deg_kinds = ("yaw", "swing", "rot", "bend", "clavicle")
        params = {}
        for n, v in zip(self.names, self.x):
            if "(log)" in n:
                params[n.replace("(log)", "")] = round(float(np.exp(v)), 4)
            elif any(k in n for k in deg_kinds):
                params[n + " [deg]"] = round(float(np.degrees(v)), 2)
            else:
                params[n] = round(float(v), 4)
        return {"rms_body_to_model_before": round(self.rms_before, 4), "rms_body_to_model_after": round(self.rms_after, 4),
                "median_model_to_body_before": round(self.model_to_body_before, 4),
                "median_model_to_body_after": round(self.model_to_body_after, 4),
                "nfev": self.nfev, "seconds": round(self.seconds, 2), "message": self.message, "params": params,
                "stages": self.stages}


def dense_lbs(points: np.ndarray, W: np.ndarray, A: np.ndarray) -> np.ndarray:
    M = np.einsum("sb,bij->sij", W, A[:, :3, :])
    return np.einsum("sij,sj->si", M[:, :, :3], points) + M[:, :, 3]


def _robust(d: np.ndarray, c: float) -> np.ndarray:
    """符号付き距離 d → 符号付きの Cauchy 残差（r² = c² log(1 + d²/c²)）。"""
    d2 = d * d
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(d2 > 1e-24, np.sqrt(c * c * np.log1p(d2 / (c * c)) / d2), 1.0)
    return d * f


def identity_fit(body: SourceBody) -> FitResult:
    n = len(body.skeleton)
    A = np.repeat(np.eye(4)[None], n, axis=0)
    return FitResult([], np.zeros(0), A, float("nan"), float("nan"), float("nan"), float("nan"), message="fit=none")


# 左右の脚の関節の水平距離の下限（身長比）。脚をそろえて立つ入力で、合わせ込んだ基準ボディの両脚が重なって
# （足首やつま先が交差して）ウェイトが左右逆に付くのを防ぐ（B-001）。値は基準ボディの脚の太さ（膝 約 0.06、足首 約 0.045）の 2 倍弱
LEG_SEPARATION = (("Bip01 L Calf", "Bip01 R Calf", 0.045), ("Bip01 L Foot", "Bip01 R Foot", 0.035),
                  ("Bip01 L Toe0", "Bip01 R Toe0", 0.035))


def fit_pose(body: SourceBody, P: np.ndarray, F: np.ndarray, *, samples_body: int = 2500, samples_model: int = 4000,
             max_nfev: int = 60, robust_c: float | None = None, reg_weight: float = 0.01,
             leg_separation: bool = True, seed: int = 12345, log=None) -> FitResult:
    """基準ボディを入力 (P, F)（事前合わせ済み）に重ねる姿勢・比率を求める。

    ``leg_separation``: 通常どおり合わせ込んだ結果、左右の膝・足首・つま先が重なる（水平距離が :data:`LEG_SEPARATION`
    （身長比）未満）か交差する（左の関節が右の関節より下限の半分以上 左側に無い）ときだけ、それを罰則にして全パラメーターを
    合わせ直す（そうでない入力の結果は従来と同じ）。
    ``seed``: 表面の点を選ぶ乱数の種（同じ種なら結果は同じ。種を変えたときのばらつきの評価用）。
    """
    t0 = time.time()
    rng = np.random.default_rng(seed)
    pm = PoseModel(body.skeleton, default_fit_spec())
    Wb = body.dense_weights()
    bfn = geometry.face_normals(body.positions, body.faces)
    bp, bfi, bbary = geometry.sample_surface(body.positions, body.faces, samples_body, rng)
    btri = body.faces[bfi]
    Ws = (bbary[:, 0:1] * Wb[btri[:, 0]] + bbary[:, 1:2] * Wb[btri[:, 1]] + bbary[:, 2:3] * Wb[btri[:, 2]])
    bn = bfn[bfi]
    gfn = geometry.face_normals(P, F)
    gp_dense, gfi_dense, _ = geometry.sample_surface(P, F, max(60000, samples_model * 10), rng)
    gn_dense = gfn[gfi_dense]
    tree = cKDTree(gp_dense)
    gp, gfi, _ = geometry.sample_surface(P, F, samples_model, rng)
    height = float(body.positions[:, 1].max() - body.positions[:, 1].min())
    c = robust_c if robust_c else 0.02 * height
    ns, ng = len(bp), len(gp)
    count = [0]
    sk = body.skeleton
    sep_pairs = [(sk.index(a), sk.index(b), f * height) for a, b, f in LEG_SEPARATION
                 if leg_separation and sk.has(a) and sk.has(b)]

    hips = (sk.index("Bip01 L Thigh"), sk.index("Bip01 R Thigh")) if sk.has("Bip01 L Thigh") and sk.has("Bip01 R Thigh") else None

    def joint(A, i):
        return A[i][:3, :3] @ sk.joints[i] + A[i][:3, 3]

    def lateral(A):
        """合わせ込んだ左右の股関節を結ぶ水平の単位ベクトル（キャラクターの左向き）。"""
        if hips is None:
            return np.array([1.0, 0.0, 0.0])
        d = joint(A, hips[0]) - joint(A, hips[1])
        d[1] = 0.0
        n = float(np.linalg.norm(d))
        return d / n if n > 1e-9 else np.array([1.0, 0.0, 0.0])

    def sep_values(A):
        """関節の組ごとに (水平距離, 左右方向の符号付き距離 = 左が左側にあれば正)。"""
        lat = lateral(A)
        out = []
        for i, j, smin in sep_pairs:
            d = joint(A, i) - joint(A, j)
            out.append((float(np.hypot(d[0], d[2])), float(d @ lat), smin))
        return out

    def sep_terms(A):
        # 重ならない（水平距離 ≥ 下限）、かつ交差しない（左の関節が右の関節より下限の半分以上 左側）。
        # 5 cm 足りないと 0.01（データ項の全体と同程度）
        out = []
        for dh, dl, smin in sep_values(A):
            out.append(2.0 * max(0.0, smin - dh))
            out.append(2.0 * max(0.0, 0.5 * smin - dl))
        return np.array(out)

    def data_terms(x, A=None):
        A = pm.transforms(x) if A is None else A
        M = np.einsum("sb,bij->sij", Ws, A[:, :3, :])
        sp = np.einsum("sij,sj->si", M[:, :, :3], bp) + M[:, :, 3]
        sn = np.einsum("sij,sj->si", M[:, :, :3], bn)
        sn /= np.maximum(np.linalg.norm(sn, axis=1, keepdims=True), 1e-12)
        _, i1 = tree.query(sp)
        d1 = np.einsum("ij,ij->i", gp_dense[i1] - sp, gn_dense[i1])          # 点→面（入力側の法線）
        _, i2 = cKDTree(sp).query(gp)
        d2 = np.einsum("ij,ij->i", gp - sp[i2], sn[i2])                       # 点→面（ボディ側の法線）
        return d1, d2, sp

    use_sep = [False]

    def residuals(x):
        count[0] += 1
        A = pm.transforms(x)
        d1, d2, _ = data_terms(x, A)
        parts = [_robust(d1, c) / np.sqrt(ns), _robust(d2, c) / np.sqrt(ng), reg_weight * pm.regularization(x)]
        if use_sep[0]:
            parts.append(sep_terms(A))
        return np.concatenate(parts)

    def cost(x):
        r = residuals(x)
        return float(r @ r)

    def stats(x):
        A = pm.transforms(x)
        sp = dense_lbs(bp, Ws, A)
        dd1, _ = tree.query(sp)
        dd2, _ = cKDTree(sp).query(gp)
        return float(np.sqrt(np.mean(dd1 ** 2))), float(np.median(dd2))

    x = pm.x0()
    names = pm.names()
    idx = {n: i for i, n in enumerate(names)}
    rms0, med0 = stats(x)
    stages = []

    def run(free: list[str], nfev: int, label: str):
        nonlocal x
        fi = [idx[n] for n in free if n in idx]
        if not fi:
            return

        def sub(z):
            xx = x.copy()
            xx[fi] = z
            return residuals(xx)
        res = optimize.least_squares(sub, x[fi], method="trf", diff_step=1e-3, max_nfev=nfev, x_scale=0.1)
        x[fi] = res.x
        stages.append({"stage": label, "cost": round(float(2 * res.cost), 8), "nfev": int(res.nfev)})
        if log:
            log(tr(f"    合わせ込み {label}: cost {2 * res.cost:.6f}（{res.nfev} 回）",
                   f"    Fitting {label}: cost {2 * res.cost:.6f} ({res.nfev} evaluations)"))

    def grid(name: str, values):
        nonlocal x
        if name not in idx:
            return
        k = idx[name]
        best = (cost(x), x[k])
        for v in values:
            xx = x.copy()
            xx[k] = v
            cv = cost(xx)
            if cv < best[0]:
                best = (cv, v)
        x[k] = best[1]

    glob = ["global scale(log)", "global yaw", "global tx", "global ty", "global tz"]
    run(glob, 30, tr("1 全体", "1 global"))
    # 腕と脚の開き（swing0 ≈ 前後軸回り）を粗く探索して初期値にする（局所解を避ける）
    for side in ("L", "R"):
        grid(f"{side} upper arm swing0", np.radians(np.arange(-60, 31, 6)) * (1 if side == "L" else -1))
        grid(f"{side} thigh swing0", np.radians(np.arange(-20, 21, 4)) * (1 if side == "L" else -1))
    stages.append({"stage": tr("2 腕・脚の開きの粗探索", "2 coarse search of the arm and leg spread"),
                   "cost": round(cost(x), 8)})
    run(glob + ["torso length(log)", "spine bend x", "spine bend z", "shoulder width(log)", "neck length(log)"]
        + [n for n in names if "swing0" in n and ("upper arm" in n or "thigh" in n)], 40,
        tr("3 胴と開き", "3 torso and spread"))
    run(names, max_nfev, tr("4 全パラメーター", "4 all parameters"))

    keys = [sk.names[i].replace("Bip01 L ", "") for i, j, smin in sep_pairs]

    def separations(A):
        return {k: v for k, v in zip(keys, sep_values(A))}
    sep0 = separations(pm.transforms(x))
    violated = any(dh < smin or dl < 0.5 * smin for dh, dl, smin in sep0.values())
    if violated:
        use_sep[0] = True
        run(names, max_nfev, tr("5 脚の重なりの解消", "5 separating overlapping legs"))
    rms1, med1 = stats(x)
    A = pm.transforms(x)
    sep1 = separations(A)
    stages.append({"stage": tr("脚の関節の水平距離", "horizontal distance of the leg joints"), "applied": bool(violated),
                   "separation_before": {k: round(v[0], 4) for k, v in sep0.items()},
                   "lateral_before": {k: round(v[1], 4) for k, v in sep0.items()},
                   "separation": {k: round(v[0], 4) for k, v in sep1.items()},
                   "lateral": {k: round(v[1], 4) for k, v in sep1.items()},
                   "minimum": {k: round(smin, 4) for k, (i, j, smin) in zip(keys, sep_pairs)}})
    return FitResult(names, x, A, rms0, rms1, med0, med1, nfev=count[0], seconds=time.time() - t0,
                     message="ok", stages=stages)


# ----------------------------------------------------------------------
# ウェイト転写
# ----------------------------------------------------------------------
@dataclass
class TransferResult:
    bone_idx: np.ndarray             # (n, 4) 出力ボーン表の番号（分割頂点ごと）
    weights: np.ndarray              # (n, 4)
    distance: np.ndarray             # (nw,) 溶接頂点 → ボディ表面の距離
    reliable: np.ndarray             # (nw,) bool
    info: dict


def _drop_small_islands(F, n, reliable, labels, min_size, fraction) -> np.ndarray:
    """確かな頂点だけでつながった島のうち、小さいものを返す（bool）。連結成分ごとに最大の島は残す。"""
    from scipy.sparse.csgraph import connected_components as cc
    f = F[reliable[F].all(axis=1)]
    i = np.concatenate([f[:, 0], f[:, 1], np.arange(n)])
    j = np.concatenate([f[:, 1], f[:, 2], np.arange(n)])
    _, lab = cc(sparse.coo_matrix((np.ones(len(i)), (i, j)), shape=(n, n)), directed=False)
    lab = np.where(reliable, lab, -1)
    if not reliable.any():
        return np.zeros(n, dtype=bool)
    sizes = np.bincount(lab[lab >= 0], minlength=lab.max() + 1)
    comp_size = np.bincount(labels, minlength=labels.max() + 1)
    # 島 → 連結成分、成分ごとの最大の島
    island_comp = np.full(len(sizes), -1)
    island_comp[lab[reliable]] = labels[reliable]
    biggest = {}
    for isl in np.flatnonzero(sizes):
        c = island_comp[isl]
        if c not in biggest or sizes[isl] > sizes[biggest[c]]:
            biggest[c] = isl
    keep = np.zeros(len(sizes), dtype=bool)
    for isl in np.flatnonzero(sizes):
        c = island_comp[isl]
        keep[isl] = isl == biggest[c] or sizes[isl] >= max(min_size, fraction * comp_size[c])
    return reliable & ~keep[np.maximum(lab, 0)]


def _smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _geodesic_from(P, F, sources: np.ndarray, unreachable: float = 0.0) -> np.ndarray:
    """``sources`` の頂点からメッシュの辺に沿った最短距離。届かない頂点は ``unreachable``（既定 0）。"""
    from scipy.sparse.csgraph import dijkstra
    n = len(P)
    src = np.flatnonzero(sources)
    if len(src) == 0:
        return np.full(n, unreachable)
    a = np.concatenate([F[:, 0], F[:, 1], F[:, 2]])
    b = np.concatenate([F[:, 1], F[:, 2], F[:, 0]])
    e = np.unique(np.concatenate([np.stack([a, b], 1), np.stack([b, a], 1)]), axis=0)   # 両向き・重複なし
    w = np.linalg.norm(P[e[:, 0]] - P[e[:, 1]], axis=1) + 1e-12
    # 仮想の始点 n から全ての確かな頂点へ長さほぼ 0 の辺を足す
    i = np.concatenate([e[:, 0], np.full(len(src), n)])
    j = np.concatenate([e[:, 1], src])
    ww = np.concatenate([w, np.full(len(src), 1e-12)])
    G = sparse.coo_matrix((ww, (i, j)), shape=(n + 1, n + 1)).tocsr()
    d = dijkstra(G, directed=True, indices=n)[:n]
    d[~np.isfinite(d)] = unreachable
    return d


# 四肢の鎖（ボーン群と、種の頂点を選ぶ中間の関節）。左右の脚、左右の腕。
LIMB_CHAINS = {
    "L leg": (("Bip01 L Thigh", "Bip01 L Calf", "Bip01 L Foot", "Bip01 L Toe0"), "Bip01 L Calf", "leg"),
    "R leg": (("Bip01 R Thigh", "Bip01 R Calf", "Bip01 R Foot", "Bip01 R Toe0"), "Bip01 R Calf", "leg"),
    "L arm": (("Bip01 L UpperArm", "Bip01 L Forearm", "Bip01 L ForeTwist", "Bip01 L Hand", "Bip01 L Finger0",
               "Bip01 L Finger1", "Bone_Shoulder_L"), "Bip01 L Forearm", "arm"),
    "R arm": (("Bip01 R UpperArm", "Bip01 R Forearm", "Bip01 R ForeTwist", "Bip01 R Hand", "Bip01 R Finger0",
               "Bip01 R Finger1", "Bone_Shoulder_R"), "Bip01 R Forearm", "arm"),
}


def limb_gates(Pw, Fw, W, reliable, labels, body: SourceBody, transforms, height: float,
               seed_radius: float = 0.07, margin: tuple[float, float] = (0.04, 0.10), ratio: float = 1.5,
               other_ratio: tuple[float, float] = (1.8, 2.5)):
    """四肢の鎖ごとに「空間では近いが表面に沿うと遠い頂点」を見つけ、その鎖のウェイトを消す係数を返す（B-001）。

    脚をそろえて立つ体では、片方の脚の内側が、合わせ込んだ基準ボディのもう片方の脚に最も近くなり、
    最近点の転写でウェイトが左右逆に付く（バインド姿勢に戻すと足元が X 字に裂ける）。手が太ももに触れる、
    腕が胴に付く、も同じ形の誤り。鎖ごとに、中間の関節（膝・肘）の近くでその鎖のウェイトが主の確かな頂点を種にして、
    メッシュの辺に沿った距離 d_geo と直線距離 d_euc を求め、``d_geo − ratio·d_euc`` が ``margin``（身長比）を超える頂点の
    鎖のウェイトを滑らかに消す。ただし **別の鎖（ほかの脚・腕）の種のほうが表面に沿って ``margin`` 以上、かつ
    ``other_ratio``（1.8〜2.5 倍で滑らかに）以上近い** 頂点に限る（その頂点ははっきり別の四肢の一部）。両脚にまたがるスカート・
    腰布・前垂れ・マントは両脚の種からの距離の比が 1.2〜2.0 程度なので消さない（脚をそろえた体のブーツの内側は 2.7 倍以上）。種のある連結成分の外は、反対側の鎖の種がある成分なら消す（ただし脚の鎖では、中線から自分の側へ 2% を超え、
    反対側の種から表面に沿って ``margin[1]`` より遠く、自分の膝より上の頂点は残す。両脚にまたがる別部品の衣装で、片側の裾
    だけが膝の近くまで下がる場合）。どちらの種も無い成分
    （体から離れたブーツなど）は、脚の鎖に限り、合わせ込んだ左右の股関節の中線に対してその成分の重心がある側の鎖だけを残す
    （今のウェイトは誤っている可能性があるので使わない）。腕の鎖はこの判定をしない（握った別部品の武器が体の中線をまたぐと、
    握る手の鎖を武器全体から消してしまう）。戻り値 ``(gates, info)``: gates は {鎖の名前: (ボーン番号の配列,
    係数 (nw,)。1 = 消す)}。
    """
    from scipy.spatial import cKDTree as _KD
    sk = body.skeleton
    m0, m1 = margin[0] * height, margin[1] * height
    r_seed = seed_radius * height
    tot = np.maximum(W.sum(axis=1), 1e-12)
    chains = {}
    for name, (bones, mid, kind) in LIMB_CHAINS.items():
        ids = np.array([sk.index(b) for b in bones if sk.has(b)], dtype=np.int64)
        if not sk.has(mid) or not len(ids):
            continue
        i = sk.index(mid)
        J = transforms[i][:3, :3] @ sk.joints[i] + transforms[i][:3, 3]
        chains[name] = (ids, J, kind)
    # 左右の中線（合わせ込んだ股関節の中点の x）。種の無い成分の左右の判定に使う（脚だけ）
    mid_x = {}
    a, b = "Bip01 L Thigh", "Bip01 R Thigh"
    if sk.has(a) and sk.has(b):
        ja, jb = sk.index(a), sk.index(b)
        xa = (transforms[ja][:3, :3] @ sk.joints[ja] + transforms[ja][:3, 3])[0]
        xb = (transforms[jb][:3, :3] @ sk.joints[jb] + transforms[jb][:3, 3])[0]
        mid_x["leg"] = (0.5 * (xa + xb), 1.0 if xa > xb else -1.0)      # (中線, L 側の符号)
    gates, info = {}, {}
    n = len(Pw)
    seeds_of = {}
    for name, (ids, J, kind) in chains.items():
        share = W[:, ids].sum(axis=1) / tot
        d_own = np.linalg.norm(Pw - J, axis=1)
        closer = np.ones(n, dtype=bool)
        for other, (_ids2, J2, kind2) in chains.items():
            if other != name and kind2 == kind:
                closer &= d_own + 0.02 * height < np.linalg.norm(Pw - J2, axis=1)
        seeds_of[name] = reliable & (d_own < r_seed) & (share > 0.5) & closer
    active = {name: seeds_of[name] for name in chains if seeds_of[name].sum() >= 5}
    geo = {name: _geodesic_from(Pw, Fw, seeds, unreachable=np.inf) for name, seeds in active.items()}
    for name, (ids, J, kind) in chains.items():
        seeds = seeds_of[name]
        if name not in active:
            info[name] = {"seeds": int(seeds.sum()), "skipped": True}
            continue
        d_geo = geo[name]
        d_euc, _ = _KD(Pw[seeds]).query(Pw)
        dg = np.where(np.isfinite(d_geo), d_geo, 0.0)
        g = _smoothstep((dg - ratio * d_euc - m0) / max(m1 - m0, 1e-9))
        # 別の鎖の種のほうが表面に沿って近い頂点だけ（その四肢の一部）
        others = [geo[o] for o in active if o != name]
        if others:
            d_other = np.min(np.stack(others), axis=0)
            g = g * _smoothstep((dg - d_other - m0) / max(m1 - m0, 1e-9))
            with np.errstate(divide="ignore", invalid="ignore"):
                rel = np.where(d_other > 0, dg / d_other, np.inf)
            g = g * _smoothstep((rel - other_ratio[0]) / max(other_ratio[1] - other_ratio[0], 1e-9))
        else:
            g = np.zeros(n)
        # 種のある連結成分に届かない頂点（別の連結成分）
        far = ~np.isfinite(d_geo)
        if far.any():
            opp = [o for o, (_i, _J, k) in chains.items() if o != name and k == kind]
            for c in np.unique(labels[far]):
                sel = labels == c
                if any(seeds_of[o][sel].any() for o in opp):
                    g[sel] = 1.0                      # 反対側の鎖の部位
                    if kind in mid_x:
                        # 脚: 両脚にまたがる成分（片側の裾だけ膝の近くまで下がる別部品のスカート・腰鎧、片脚だけ膝の上で
                        # 体と離れた体）は、成分のうち中線から自分の側へ 2% を超え、反対側の種から表面に沿って margin[1] より
                        # 遠く、自分の膝より上の頂点を残す（膝より下は、足を交差させた反対側のブーツの先を消すため従来どおり）
                        mx, lsign = mid_x[kind]
                        offv = (Pw[sel, 0] - mx) * lsign
                        mine = offv if name.startswith("L") else -offv
                        ns = int(sel.sum())
                        dop = np.min(np.stack([geo[o][sel] if o in geo else np.full(ns, np.inf) for o in opp]), axis=0)
                        keep = (mine > 0.02 * height) & (dop > m1) & (Pw[sel, 1] > J[1])
                        gs = np.ones(ns)
                        gs[keep] = 0.0
                        g[sel] = gs
                elif kind in mid_x:
                    mx, lsign = mid_x[kind]
                    off = (float(Pw[sel, 0].mean()) - mx) * lsign      # L 側なら正
                    mine = off if name.startswith("L") else -off
                    g[sel] = 1.0 if mine < -0.02 * height else 0.0     # 反対側に 2% 以上寄った成分だけ消す
                else:
                    g[sel] = 0.0                      # 腕（握った武器は中線をまたいでも手に付ける）・股関節の無い骨格
        gates[name] = (ids, g)
        info[name] = {"seeds": int(seeds.sum()), "gated_vertices": int((g > 0.5).sum()),
                      "removed_weight": round(float((W[:, ids].sum(axis=1) * g).sum()), 2)}
    return gates, info


def _apply_gates(W, gates):
    for ids, g in gates.values():
        W[:, ids] *= (1.0 - g)[:, None]
    return W


def transfer_weights(body: SourceBody, transforms: np.ndarray, P: np.ndarray, F: np.ndarray, remap: np.ndarray,
                     reps: np.ndarray, *, max_distance: float, max_angle_deg: float = 60.0,
                     smooth_iterations: int = 3, max_influences: int = 4, min_weight: float = 0.01,
                     island_min: int = 50, island_fraction: float = 0.005,
                     leg_to_pelvis: tuple[float, float] | None = None, head_rigid: bool = False,
                     limb_gating: bool = False, height: float | None = None) -> TransferResult:
    """溶接した位相（remap/reps は :func:`geometry.weld` の結果）でウェイトを決め、分割頂点に配る。

    ``leg_to_pelvis = (g0, g1)``: 確かな頂点からの表面距離が g0〜g1 の補間部分で、脚のウェイトを
    骨盤へ滑らかに移す（g1 以上で全部）。None なら行わない。
    ``head_rigid``: 補間部分のうち頭のウェイトが 35% を超える頂点を、65% 以上で頭 100% になるよう寄せる。
    ``limb_gating``: 四肢の取り違え（脚をそろえた体の左右逆、手と太もも）を :func:`limb_gates` で消す。
    ``height`` は基準ボディの身長（省略時は基準ボディから計算）。
    """
    Pw = np.asarray(P, dtype=np.float64)[reps]
    Fw = remap[np.asarray(F)]
    ok = (Fw[:, 0] != Fw[:, 1]) & (Fw[:, 1] != Fw[:, 2]) & (Fw[:, 0] != Fw[:, 2])
    Fw = Fw[ok]
    nw = len(Pw)
    nb = len(body.skeleton)
    Nw = geometry.vertex_normals(Pw, Fw)
    # 合わせた姿勢の基準ボディ
    posed = lbs(body.positions, body.bone_idx, body.weights, transforms)
    q = geometry.SurfaceQuery(posed, body.faces)
    fidx, bary, _, dist = q.query(Pw, k=16)
    Wb = body.dense_weights()
    tri = body.faces[fidx]
    W = bary[:, 0:1] * Wb[tri[:, 0]] + bary[:, 1:2] * Wb[tri[:, 1]] + bary[:, 2:3] * Wb[tri[:, 2]]
    cosang = np.einsum("ij,ij->i", Nw, q.face_normals[fidx])
    reliable = (dist <= max_distance) & (cosang >= np.cos(np.radians(max_angle_deg)))
    labels = geometry.connected_components(Fw, nw)
    # 確かな頂点の「島」のうち小さいもの（尻尾の先がたまたま足に近い、など）は捨てる。
    # ただしメッシュの連結成分ごとに最大の島は残す
    dropped = _drop_small_islands(Fw, nw, reliable, labels, island_min, island_fraction)
    reliable = reliable & ~dropped
    gates, gate_info = {}, {}
    gated_unreliable = 0
    lost = np.zeros(nw)
    if limb_gating:
        H = body.height if height is None else height
        gates, gate_info = limb_gates(Pw, Fw, W, reliable, labels, body, transforms, H)
        before = W.sum(axis=1)
        W = _apply_gates(W, gates)
        lost = 1.0 - W.sum(axis=1) / np.maximum(before, 1e-12)
        # 消した量が多い頂点は、その部位の確かな頂点からの補間に回す
        bad = reliable & (lost > 0.3)
        gated_unreliable = int(bad.sum())
        reliable = reliable & ~bad
    # 確かな頂点が 1 つも無い連結成分は、最近点のウェイトをそのまま使う（浮いた部品など）
    has_rel = np.zeros(labels.max() + 1, dtype=bool)
    np.logical_or.at(has_rel, labels, reliable)
    orphan = ~has_rel[labels]
    # ただしゲートで消した量が多い頂点は、同じ成分のゲートで消えなかった頂点から補間する
    # （そのまま使うと、消した鎖のウェイトしか無かった頂点が全て 0 になる）。成分全体が消えた場合は最後に骨盤に付ける
    gone = orphan & (lost > 0.3)
    has_kept = np.zeros(labels.max() + 1, dtype=bool)
    np.logical_or.at(has_kept, labels, orphan & ~gone)
    orphan_gated = gone & has_kept[labels]
    known = (reliable | orphan) & ~orphan_gated
    L = geometry.uniform_laplacian(Fw, nw)
    unknown = np.flatnonzero(~known)
    geo = np.zeros(nw)
    moved = 0
    head_moved = 0
    if len(unknown):
        kn = np.flatnonzero(known)
        cols = np.flatnonzero(W[kn].sum(axis=0) > 0)
        Luu = L[unknown][:, unknown].tocsc()
        Luk = L[unknown][:, kn]
        rhs = -(Luk @ W[np.ix_(kn, cols)])
        # 孤立した頂点（辺が無い）は対角 0 になるので小さな正則化を足す
        Luu = Luu + sparse.identity(len(unknown), format="csc") * 1e-9
        sol = splu(Luu).solve(np.asarray(rhs))
        Wu = np.zeros((len(unknown), nb))
        Wu[:, cols] = sol
        W[unknown] = Wu
        W = np.clip(W, 0.0, None)
        if gates:
            W = _apply_gates(W, gates)
        # 体から（表面に沿って）離れた補間部分では、脚のボーンのウェイトを骨盤に移す。
        # 尻尾・腰から下がる布・浮いた装飾が歩くたびに脚と一緒に振り回されないようにする。
        # 腕のボーンは移さない（手に持った物が手に付いて動くように）。
        # 補間結果に胴のボーン（骨盤・背骨）が混ざっている部分だけが対象（胴から生えた物）。
        # 足先の爪のように脚だけから補間された部分は対象外（足に付いて動くべき物）。
        if leg_to_pelvis and body.skeleton.has("Bip01 Pelvis"):
            geo = _geodesic_from(Pw, Fw, known)
            g0, g1 = leg_to_pelvis
            a = _smoothstep((geo - g0) / max(g1 - g0, 1e-9))
            sk = body.skeleton
            legs = [i for i, n in enumerate(sk.names) if any(n.endswith(s) for s in (" Thigh", " Calf", " Foot", " Toe0"))]
            core = [sk.index(n) for n in ("Bip01 Pelvis", "Bip01 Spine", "Bip01 Spine1", "Bip01") if sk.has(n)]
            share = W[:, core].sum(axis=1) / np.maximum(W.sum(axis=1), 1e-12)
            a = a * _smoothstep((share - 0.02) / 0.13)
            a[known] = 0.0
            pel = sk.index("Bip01 Pelvis")
            move = W[:, legs] * a[:, None]
            W[:, legs] -= move
            W[:, pel] += move.sum(axis=1)
            moved = int((a > 0.01).sum())
        # 頭の付属物（角・兜飾り・髪・長い鼻先）: 補間結果で頭が 35% を超える頂点を、65% 以上で頭 100% になるよう寄せる。
        # 大きな頭では確かな頂点が頭頂の一部だけになり、補間に首・背骨が混ざって（竜人の角で頭 6 割前後）、
        # 頭を動かしたときに角が曲がるため。
        if head_rigid and body.skeleton.has("Bip01 Head"):
            h = body.skeleton.index("Bip01 Head")
            W = np.clip(W, 0.0, None)
            share = W[:, h] / np.maximum(W.sum(axis=1), 1e-12)
            f = _smoothstep((share - 0.35) / 0.3)
            f[known] = 0.0
            others = W.copy()
            others[:, h] = 0.0
            mv = others * f[:, None]
            W -= mv
            W[:, h] += mv.sum(axis=1)
            head_moved = int((f > 0.01).sum())
    W = np.clip(W, 0.0, None)
    # 平滑化（隣接平均との混合）
    if smooth_iterations > 0:
        A = (-L).tolil()
        A.setdiag(0)
        A = A.tocsr()
        deg = np.asarray(A.sum(axis=1)).reshape(-1)
        deg[deg == 0] = 1.0
        for _ in range(smooth_iterations):
            W = 0.5 * W + 0.5 * (A @ W) / deg[:, None]
    s = W.sum(axis=1, keepdims=True)
    W = W / np.where(s > 0, s, 1.0)
    # 上位 max_influences 本に絞る
    k = max_influences
    order = np.argsort(-W, axis=1)[:, :k]
    top = np.take_along_axis(W, order, axis=1)
    top[top < min_weight] = 0.0
    ts = top.sum(axis=1, keepdims=True)
    bad = ts[:, 0] <= 0
    if bad.any():                     # ウェイトが全て消えた頂点（ゲートで成分全体が消えた場合など）は骨盤に 100%
        sk = body.skeleton            # （0 番のボーンはルートの Target_CTRL で、体と一緒に動かない）
        order[bad, 0] = sk.index("Bip01 Pelvis") if sk.has("Bip01 Pelvis") else int(np.argmax(W.sum(axis=0)))
        top[bad, 0] = 1.0
        ts[bad] = 1.0
    top = top / ts
    order = np.where(top > 0, order, 0)
    if k < 4:
        order = np.hstack([order, np.zeros((nw, 4 - k), np.int64)])
        top = np.hstack([top, np.zeros((nw, 4 - k))])
    used = np.unique(order[top > 0])
    info = {
        "welded_vertices": int(nw),
        "reliable_fraction": round(float(reliable.mean()), 4),
        "dropped_island_vertices": int(dropped.sum()),
        "inpainted_vertices": int(len(unknown)),
        "leg_to_pelvis_vertices": moved,
        "head_rigid_vertices": head_moved,
        "limb_gating": {"chains": gate_info, "made_unreliable": gated_unreliable,
                        "orphan_inpainted": int(orphan_gated.sum())} if limb_gating else None,
        "geodesic_max": round(float(geo.max()), 4) if len(geo) else 0.0,
        "orphan_vertices": int(orphan.sum()),
        "zero_weight_vertices": int(bad.sum()),
        "distance_median": round(float(np.median(dist)), 4),
        "distance_p95": round(float(np.percentile(dist, 95)), 4),
        "distance_max": round(float(dist.max()), 4),
        "bones_used": [body.skeleton.names[i] for i in used],
        "influences_hist": np.bincount((top > 0).sum(axis=1), minlength=5).tolist(),
    }
    return TransferResult(order[remap], top[remap].astype(np.float32), dist, reliable, info)


def unpose(P: np.ndarray, bone_idx: np.ndarray, weights: np.ndarray, transforms: np.ndarray) -> np.ndarray:
    return inverse_lbs(P, bone_idx, weights, transforms)


def _bone_share(bone_idx, weights, ids) -> np.ndarray:
    s = np.zeros(len(bone_idx))
    for b in ids:
        s += np.where(bone_idx == b, weights, 0).sum(axis=1)
    return s


def ground_snap(P: np.ndarray, bone_idx: np.ndarray, weights: np.ndarray, skel, limit: float = 0.15,
                ground: float = 0.0, mode: str = "legs") -> tuple[np.ndarray, float]:
    """足・つま先のウェイトが大きい頂点の最低点を ``ground`` に合わせる。戻り値 (頂点ごとの y 移動量, 足元での移動量)。

    * ``legs``: 脚のボーン（太もも〜つま先）のウェイトを持つ頂点だけを、地面で全量・膝の高さで 0 になるよう
      高さに応じて動かす（すねが少し縮む）。胴と手は動かないので、武器を持つ手の位置がずれない
    * ``all``: 全体を平行移動（手も動く）
    * ``none``: 動かさない
    """
    n = len(P)
    names = ("Bip01 L Foot", "Bip01 R Foot", "Bip01 L Toe0", "Bip01 R Toe0")
    feet = [skel.index(x) for x in names if skel.has(x)]
    if mode == "none" or not feet:
        return np.zeros(n), 0.0
    sel = _bone_share(bone_idx, weights, feet) > 0.5
    if not sel.any():
        return np.zeros(n), 0.0
    dy = float(np.clip(ground - float(P[sel, 1].min()), -limit, limit))
    if mode == "all":
        return np.full(n, dy), dy
    if mode != "legs":
        raise ValueError(tr(f"ground_snap の mode は legs / all / none: {mode}",
                            f"ground_snap mode must be legs / all / none: {mode}"))
    legs = [i for i, x in enumerate(skel.names) if any(x.endswith(s) for s in (" Thigh", " Calf", " Foot", " Toe0"))]
    knees = [skel.joints[skel.index(f"Bip01 {s} Calf"), 1] for s in "LR" if skel.has(f"Bip01 {s} Calf")]
    knee = float(np.mean(knees)) if knees else float(np.percentile(P[:, 1], 30))
    low = float(P[sel, 1].min())
    f = np.clip((knee - P[:, 1]) / max(knee - low, 1e-6), 0.0, 1.0)
    return dy * f * np.clip(_bone_share(bone_idx, weights, legs), 0.0, 1.0), dy
