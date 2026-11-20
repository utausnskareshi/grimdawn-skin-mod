"""変換結果の品質の目安（報告 JSON の ``quality``）。どれもゲームの挙動そのものではなく、確認画像と合わせて見るための数値。

* :func:`limb_angles` — 上腕・前腕・太ももの鉛直からの角度（バインド姿勢と、合わせ込みで推定した入力の姿勢）
* :func:`hand_to_weapon` — 手・指のウェイトが 5 割を超える頂点の重心と、武器ボーン（``Bip01 L/R Weapon``）の距離
* :func:`test_pose_stretch` — 試験姿勢（:func:`skeleton.test_pose_spec`）で変形させたときの辺の長さの比
* :func:`far_part_shares` — 基準ボディから離れた部分（尻尾・翼・マントの裾など）のウェイトの内訳
"""
from __future__ import annotations

import numpy as np

from . import geometry
from .skeleton import PoseModel, lbs, test_pose_spec

__all__ = ["limb_angles", "hand_to_weapon", "test_pose_stretch", "far_part_shares", "BONE_GROUPS", "bone_group"]

BONE_GROUPS = ("legs", "pelvis_spine", "chest_neck", "arms", "hands", "head", "other")


def bone_group(name: str) -> str:
    if any(name.endswith(s) for s in (" Thigh", " Calf", " Foot", " Toe0")):
        return "legs"
    if name in ("Bip01", "Bip01 Pelvis", "Bip01 Spine"):
        return "pelvis_spine"
    if name in ("Bip01 Spine1", "Bip01 Neck") or name.endswith(" Clavicle"):
        return "chest_neck"
    if any(name.endswith(s) for s in (" UpperArm", " Forearm", " ForeTwist")) or name.startswith("Bone_Shoulder"):
        return "arms"
    if " Hand" in name or " Finger" in name or "Weapon" in name:
        return "hands"
    if name == "Bip01 Head":
        return "head"
    return "other"


def limb_angles(skel, transforms: np.ndarray | None = None) -> dict:
    """鉛直から測った角度（度）。``transforms`` を渡すとその姿勢の関節で測る。"""
    n = len(skel)
    A = np.repeat(np.eye(4)[None], n, axis=0) if transforms is None else transforms
    J = np.array([A[i][:3, :3] @ skel.joints[i] + A[i][:3, 3] for i in range(n)])
    out = {}
    for s in "LR":
        for a, b, lab in (("UpperArm", "Forearm", "upper_arm"), ("Forearm", "Hand", "forearm"), ("Thigh", "Calf", "thigh")):
            na, nb = f"Bip01 {s} {a}", f"Bip01 {s} {b}"
            if skel.has(na) and skel.has(nb):
                d = J[skel.index(nb)] - J[skel.index(na)]
                out[f"{s} {lab}"] = round(float(np.degrees(np.arctan2(np.hypot(d[0], d[2]), -d[1]))), 1)
    return out


def hand_to_weapon(P: np.ndarray, bone_idx: np.ndarray, weights: np.ndarray, skel) -> dict:
    out = {}
    for s in "LR":
        ids = [i for i, n in enumerate(skel.names) if n.startswith(f"Bip01 {s} Hand") or n.startswith(f"Bip01 {s} Finger")]
        w = f"Bip01 {s} Weapon"
        if not ids or not skel.has(w):
            continue
        share = sum(np.where(bone_idx == b, weights, 0).sum(axis=1) for b in ids)
        sel = share > 0.5
        # 手のウェイトが 5 割を超える頂点が無い（手に持った物で手首から先が補間になった等）ときは None
        out[s] = round(float(np.linalg.norm(P[sel].mean(axis=0) - skel.joints[skel.index(w)])), 4) if sel.any() else None
    return out


def test_pose_stretch(P: np.ndarray, F: np.ndarray, bone_idx: np.ndarray, weights: np.ndarray, skel) -> dict:
    spec, vals = test_pose_spec()
    pm = PoseModel(skel, spec)
    A = pm.transforms(np.array([vals[p.label] for p in pm.spec]))
    Q = lbs(P, bone_idx, weights, A)
    e0 = geometry.edge_lengths(P, F).reshape(-1)
    e1 = geometry.edge_lengths(Q, F).reshape(-1)
    ok = e0 > 1e-9
    r = e1[ok] / e0[ok]
    return {"p01": round(float(np.percentile(r, 1)), 3), "p99": round(float(np.percentile(r, 99)), 3),
            "max": round(float(r.max()), 3), "frac_over_1.5": round(float((r > 1.5).mean()), 5),
            "frac_under_0.67": round(float((r < 1 / 1.5).mean()), 5)}


# 警告の閾値。2026-10-16 の 11 例（竜人 3 種、テキスト→3D 8 体）と公式の素体から決めた目安（docs/converter.md 8 章）
FLAG_LIMITS = {
    "fit_rms_after": 0.06,          # 合わせ込み後の RMS（基準ボディ → 入力）。良好な例は 0.024〜0.05
    "below_ground": -0.05,          # 最低点 y。翼の先・下ろした腕と一緒に回った剣が地面より下に出る
    "hand_to_weapon": 0.25,         # 手の頂点の重心と武器ボーンの距離。公式素体 0.02〜0.11、竜人 0.09〜0.25
    "stretch_over_1.5": 0.02,       # 試験姿勢で 1.5 倍を超えて伸びる辺の割合。公式素体 0.4%、竜人 0.6%
    "far_fraction": 0.3,            # 基準ボディから離れた頂点の割合（翼・大きなマントなど）
    # 変換（バインド姿勢へ戻す工程）で 2 倍を超えて伸びた辺の割合。既存の 11 例は全て 0.1% 未満（99% 点 ≤ 1.44）、
    # B-001 の少女（改善前）は男性用 2.4%（99% 点 3.54）・女性用 1.0%（2.06）
    "conversion_over_2": 0.005,
}


def flags(fit_rms_after: float | None, bbox_min_y: float, hand: dict, stretch: dict, far: dict,
          edge_ratio: dict | None = None, zero_weight_vertices: int = 0, outlier_vertices: int = 0) -> list[str]:
    """品質の目安から、確認画像で見直すべき点を短い英語のキーで返す（空なら目立った問題なし）。

    ``edge_ratio``（入力 → バインド姿勢の辺の長さの比、``frac_over_2`` を含む）を渡すと、変換そのものでの崩れ
    ``strong_stretch_in_conversion`` も判定する。``zero_weight_vertices``（ウェイトが全て消えて骨盤に 100% を
    付けた頂点の数。rigging.transfer_weights の info）が 1 以上なら ``zero_weight_vertices``（点検 R03A-01）。
    ``outlier_vertices``（体から大きく離れ、大きさと位置の合わせ込みから除いた頂点の数。rigging.Prealign）が 1 以上なら
    ``outlier_vertices``（決定 #2 R03B-03。その部品は出力に残る）。
    """
    out = []
    if fit_rms_after is not None and fit_rms_after > FLAG_LIMITS["fit_rms_after"]:
        out.append("weak_pose_fit")
    if bbox_min_y < FLAG_LIMITS["below_ground"]:
        out.append("below_ground")
    for s in "LR":
        if s not in hand:          # 武器ボーンの無い骨格（合成データなど）
            continue
        if hand[s] is None:
            out.append(f"no_hand_weights_{s}")
        elif hand[s] > FLAG_LIMITS["hand_to_weapon"]:
            out.append(f"hand_far_from_weapon_{s}")
    if stretch.get("frac_over_1.5", 0) > FLAG_LIMITS["stretch_over_1.5"]:
        out.append("strong_stretch_in_test_pose")
    if far.get("fraction", 0) > FLAG_LIMITS["far_fraction"]:
        out.append("large_parts_away_from_body")
    if edge_ratio is not None and edge_ratio.get("frac_over_2", 0) > FLAG_LIMITS["conversion_over_2"]:
        out.append("strong_stretch_in_conversion")
    if zero_weight_vertices > 0:
        out.append("zero_weight_vertices")
    if outlier_vertices > 0:
        out.append("outlier_vertices")
    return out


def far_part_shares(P: np.ndarray, bone_idx: np.ndarray, weights: np.ndarray, body, threshold: float) -> dict:
    """バインド姿勢の基準ボディ表面から ``threshold`` 以上離れた頂点のウェイトをボーン群ごとに合計した割合。"""
    q = geometry.SurfaceQuery(body.positions, body.faces)
    _, _, _, d = q.query(P, k=12)
    sel = d >= threshold
    out = {"threshold": round(float(threshold), 4), "vertices": int(sel.sum()), "fraction": round(float(sel.mean()), 4)}
    if sel.any():
        names = body.skeleton.names
        groups = np.array([BONE_GROUPS.index(bone_group(n)) for n in names])
        acc = np.zeros(len(BONE_GROUPS))
        np.add.at(acc, groups[bone_idx[sel]].reshape(-1), weights[sel].reshape(-1))
        acc /= max(acc.sum(), 1e-12)
        out["shares"] = {g: round(float(v), 3) for g, v in zip(BONE_GROUPS, acc) if v > 0}
    return out
