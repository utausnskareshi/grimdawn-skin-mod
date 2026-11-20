"""プレイヤー骨格の扱い（関節位置、姿勢の変換、線形ブレンドスキニング）。

.msh のボーン表（チャンク 6）の行列は親相対・行ベクトル規約と推定されている（docs/msh_format.md 2.6）。
ここではワールドの関節位置（各ボーンのワールド行列の平行移動）だけを使い、姿勢の変換は
**バインド空間の点 → 変形後の点** への 3×4 アフィン変換（列ベクトル規約、4×4 で保持）として自前で定義する。
出力の .msh には元のボーン表をそのまま写すので、ここでの規約はエンジンの規約と無関係。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from gdmesh import msh

from ._i18n import tr

__all__ = [
    "Skeleton", "PRIMARY_CHILD", "FIT_PARENT", "rotation_matrix", "rotation_between",
    "lbs", "inverse_lbs", "retarget_transforms", "ParamSpec", "PoseModel", "default_fit_spec",
    "test_pose_spec",
]

SIDES = ("L", "R")


def _sided(table: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in table.items():
        if "{s}" in k:
            for s in SIDES:
                out[k.format(s=s)] = v.format(s=s)
        else:
            out[k] = v
    return out


# 骨の向き（関節 → 主な子の関節）。ここに無いボーンは向きを持たない（親の変換に従う）。
PRIMARY_CHILD = _sided({
    "Bip01": "Bip01 Spine",
    "Bip01 Pelvis": "Bip01 Spine",
    "Bip01 Spine": "Bip01 Spine1",
    "Bip01 Spine1": "Bip01 Neck",
    "Bip01 Neck": "Bip01 Head",
    "Bip01 {s} Clavicle": "Bip01 {s} UpperArm",
    "Bip01 {s} UpperArm": "Bip01 {s} Forearm",
    "Bip01 {s} Forearm": "Bip01 {s} Hand",
    "Bip01 {s} ForeTwist": "Bip01 {s} Hand",
    "Bip01 {s} Hand": "Bip01 {s} Finger1",
    "Bip01 {s} Thigh": "Bip01 {s} Calf",
    "Bip01 {s} Calf": "Bip01 {s} Foot",
    "Bip01 {s} Foot": "Bip01 {s} Toe0",
})

# 姿勢合わせで使う親子関係の上書き。ゲームの骨格（3ds Max Biped）では太ももが Spine の子、
# 鎖骨が Neck の子だが、背骨を曲げたときに脚や肩が振り回されないよう、合わせ込みでは
# 太もも → Pelvis、鎖骨 → Spine1 の子として扱う。
FIT_PARENT = _sided({
    "Bip01 {s} Thigh": "Bip01 Pelvis",
    "Bip01 {s} Clavicle": "Bip01 Spine1",
})


def rotation_matrix(rotvec: np.ndarray) -> np.ndarray:
    """回転ベクトル（軸 × 角度[rad]）→ 3×3 回転行列（Rodrigues）。"""
    w = np.asarray(rotvec, dtype=np.float64)
    th = float(np.linalg.norm(w))
    if th < 1e-12:
        return np.eye(3)
    k = w / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * (K @ K)


def rotation_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """単位ベクトル a を b に重ねる最小回転。"""
    a = np.asarray(a, dtype=np.float64) / np.linalg.norm(a)
    b = np.asarray(b, dtype=np.float64) / np.linalg.norm(b)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    s = float(np.linalg.norm(v))
    if s < 1e-12:
        if c > 0:
            return np.eye(3)
        # 180°: a に直交する任意の軸で回す
        axis = np.cross(a, [1.0, 0, 0])
        if np.linalg.norm(axis) < 1e-6:
            axis = np.cross(a, [0, 1.0, 0])
        return rotation_matrix(axis / np.linalg.norm(axis) * np.pi)
    return rotation_matrix(v / s * np.arctan2(s, c))


def _affine(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = t
    return M


def _about(j: np.ndarray, L: np.ndarray) -> np.ndarray:
    """点 j を中心とする線形変換 L（3×3）の 4×4 表現: x → j + L (x - j)。"""
    return _affine(L, j - L @ j)


@dataclass
class Skeleton:
    """ボーン名・親・ワールドの関節位置。``bone_chunk`` は出力 .msh にそのまま写す元のボーン表。"""

    names: list[str]
    parents: list[int]
    joints: np.ndarray                       # (n, 3) バインド姿勢のワールド位置
    bone_chunk: msh.BoneChunk | None = None
    source: str = ""
    _index: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._index = {n: i for i, n in enumerate(self.names)}

    @classmethod
    def from_msh(cls, m: msh.MshFile, source: str = "") -> "Skeleton":
        if m.bones is None or not m.bones.bones:
            raise ValueError(tr("ボーン表のない .msh", "The .msh has no bone table"))
        parents = m.bones.parents()
        world = []
        for i, b in enumerate(m.bones.bones):
            L = np.eye(4)
            L[:3, :3] = b.matrix[:3]
            L[3, :3] = b.matrix[3]
            world.append(L if parents[i] < 0 else L @ world[parents[i]])   # 行ベクトル規約
        joints = np.array([w[3, :3] for w in world], dtype=np.float64)
        return cls([b.name for b in m.bones.bones], list(parents), joints, m.bones, source)

    def __len__(self) -> int:
        return len(self.names)

    def index(self, name: str) -> int:
        return self._index[name]

    def has(self, name: str) -> bool:
        return name in self._index

    def fit_parent(self, i: int) -> int:
        override = FIT_PARENT.get(self.names[i])
        if override and self.has(override):
            return self.index(override)
        return self.parents[i]

    def topo_order(self) -> list[int]:
        """合わせ込み用の親（:data:`FIT_PARENT`）が子より先に来る順序。"""
        n = len(self.names)
        done = [False] * n
        order: list[int] = []

        def visit(i, depth=0):
            if done[i]:
                return
            if depth > n:
                raise ValueError(tr("ボーンの親子関係が循環している", "The bone hierarchy has a cycle"))
            p = self.fit_parent(i)
            if p >= 0:
                visit(p, depth + 1)
            done[i] = True
            order.append(i)

        for i in range(n):
            visit(i)
        return order

    def direction(self, i: int) -> np.ndarray | None:
        """ボーン i の向き（関節 → 主な子の関節。単位ベクトル）。無ければ None。"""
        c = PRIMARY_CHILD.get(self.names[i])
        if not c or not self.has(c):
            return None
        d = self.joints[self.index(c)] - self.joints[i]
        ln = float(np.linalg.norm(d))
        return d / ln if ln > 1e-6 else None

    def bone_length(self, i: int) -> float:
        c = PRIMARY_CHILD.get(self.names[i])
        if not c or not self.has(c):
            return 0.0
        return float(np.linalg.norm(self.joints[self.index(c)] - self.joints[i]))

    def map_names(self, other: "Skeleton") -> np.ndarray:
        """self のボーン番号 → other のボーン番号。other に無いボーンは祖先をたどって対応させる。"""
        out = np.zeros(len(self.names), dtype=np.int64)
        for i, n in enumerate(self.names):
            j = i
            while j >= 0 and not other.has(self.names[j]):
                j = self.parents[j]
            out[i] = other.index(self.names[j]) if j >= 0 else 0
        return out


# ----------------------------------------------------------------------
# スキニング
# ----------------------------------------------------------------------
def lbs(points: np.ndarray, bone_idx: np.ndarray, weights: np.ndarray, transforms: np.ndarray) -> np.ndarray:
    """線形ブレンドスキニング: x' = Σ_k w_k A[b_k] x。``transforms`` は (nb, 4, 4)。"""
    p = np.asarray(points, dtype=np.float64)
    A = transforms[np.asarray(bone_idx)]                 # (n, k, 4, 4)
    w = np.asarray(weights, dtype=np.float64)
    M = np.einsum("nk,nkij->nij", w, A[:, :, :3, :])     # (n, 3, 4)
    return np.einsum("nij,nj->ni", M[:, :, :3], p) + M[:, :, 3]


def inverse_lbs(points: np.ndarray, bone_idx: np.ndarray, weights: np.ndarray, transforms: np.ndarray) -> np.ndarray:
    """変形後の点をバインド姿勢に戻す: x = Σ_k w_k A[b_k]^{-1} x'（逆変換のブレンド。近似）。"""
    inv = np.linalg.inv(transforms)
    return lbs(points, bone_idx, weights, inv)


def retarget_transforms(src: Skeleton, dst: Skeleton) -> np.ndarray:
    """src のバインド姿勢 → dst のバインド姿勢 へ点を写すボーンごとの変換（src のボーン番号順）。

    向きのあるボーンは「関節を合わせ、骨の向きを最小回転で合わせ、骨の長さの比で骨方向に伸縮」。
    向きの無いボーン（指先、つま先、頭、装備用ボーンなど）は親の変換に従う。
    ボーン行列の軸（ねじれ）は使わない（男女で手や背骨のローカル軸が 10〜20° 違うため）。
    """
    n = len(src)
    out = np.zeros((n, 4, 4))
    done = [False] * n
    for i in src.topo_order():
        name = src.names[i]
        d_s = src.direction(i)
        if dst.has(name) and d_s is not None:
            j = dst.index(name)
            d_d = dst.direction(j)
            if d_d is not None:
                R = rotation_between(d_s, d_d)
                s = dst.bone_length(j) / max(src.bone_length(i), 1e-9)
                S = np.eye(3) + (s - 1.0) * np.outer(d_s, d_s)
                L = R @ S
                out[i] = _affine(L, dst.joints[j] - L @ src.joints[i])
                done[i] = True
                continue
        p = src.fit_parent(i)
        if p >= 0 and done[p]:
            out[i] = out[p]
        elif dst.has(name):
            out[i] = _affine(np.eye(3), dst.joints[dst.index(name)] - src.joints[i])
        else:
            out[i] = np.eye(4)
        done[i] = True
    return out


# ----------------------------------------------------------------------
# 姿勢モデル（合わせ込み用）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ParamSpec:
    """姿勢パラメーター 1 個。

    kind:
      ``scale``  全体の一様拡大（対数）。原点中心
      ``yaw``    全体の y 軸回転 [rad]
      ``tx``/``ty``/``tz``  全体の平行移動
      ``swing``  ボーン ``bones`` を骨の向きに直交する軸 ``axis``（0/1）回りに回す [rad]
      ``rot``    ボーン ``bones`` をワールド軸 ``axis``（0=x, 1=y, 2=z）回りに回す [rad]
      ``stretch`` ボーン群 ``bones`` を骨の向きに伸縮（対数）。左右で共有するときに複数指定
    ``mirror``: 左右対称の指定で、R 側の値の符号を反転する成分（x 軸 = 左右方向の鏡像）
    """

    kind: str
    bones: tuple[str, ...] = ()
    axis: int = 0
    prior: float = 0.0          # 正則化の中心
    sigma: float = 0.5          # 正則化の幅（値の単位）
    label: str = ""

    @property
    def name(self) -> str:
        if self.label:
            return self.label
        b = "+".join(self.bones)
        return f"{self.kind}[{b}]{self.axis if self.kind in ('swing', 'rot') else ''}"


def _swing_axes(d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """骨の向き d に直交する 2 軸。e0 は z（前後）軸に近い向き、e1 = d × e0。"""
    ref = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(d, ref))) > 0.9:
        ref = np.array([1.0, 0.0, 0.0])
    e0 = ref - d * float(np.dot(d, ref))
    e0 /= np.linalg.norm(e0)
    e1 = np.cross(d, e0)
    return e0, e1


class PoseModel:
    """パラメーター列 → ボーンごとのアフィン変換（バインド空間 → 変形後）。

    各ボーン b の局所変換 M_b = 「関節 j_b 中心に R_b·S_b」（R_b = 回転、S_b = 骨方向の伸縮）。
    全体の変換は A_b = A_parent(b) ∘ M_b（親は :data:`FIT_PARENT` で上書きした合わせ込み用の親）、
    根は A = G ∘ M（G = 一様拡大・y 軸回転・平行移動）。
    """

    def __init__(self, skel: Skeleton, spec: list[ParamSpec]):
        self.skel = skel
        self.spec = [p for p in spec if all(skel.has(b) for b in p.bones)]
        self.order = skel.topo_order()
        n = len(skel)
        self.dirs = [skel.direction(i) for i in range(n)]
        self.axes = [(_swing_axes(d) if d is not None else None) for d in self.dirs]
        # パラメーター → (ボーン番号, 回転軸ベクトル) / 伸縮対象
        self._rot: list[list[tuple[int, np.ndarray]]] = []
        self._str: list[list[int]] = []
        for p in self.spec:
            rots, strs = [], []
            for bname in p.bones:
                i = skel.index(bname)
                if p.kind == "swing" and self.axes[i] is not None:
                    rots.append((i, self.axes[i][p.axis]))
                elif p.kind == "rot":
                    ax = np.zeros(3)
                    ax[p.axis] = 1.0
                    rots.append((i, ax))
                elif p.kind == "stretch" and self.dirs[i] is not None:
                    strs.append(i)
            self._rot.append(rots)
            self._str.append(strs)

    @property
    def size(self) -> int:
        return len(self.spec)

    def x0(self) -> np.ndarray:
        return np.array([p.prior for p in self.spec], dtype=np.float64)

    def names(self) -> list[str]:
        return [p.name for p in self.spec]

    def transforms(self, x: np.ndarray) -> np.ndarray:
        skel = self.skel
        n = len(skel)
        rv = np.zeros((n, 3))
        ls = np.zeros(n)
        g_scale, g_yaw, g_t = 0.0, 0.0, np.zeros(3)
        for k, p in enumerate(self.spec):
            v = float(x[k])
            if p.kind == "scale":
                g_scale += v
            elif p.kind == "yaw":
                g_yaw += v
            elif p.kind in ("tx", "ty", "tz"):
                g_t["xyz".index(p.kind[1])] += v
            elif p.kind in ("swing", "rot"):
                for i, ax in self._rot[k]:
                    rv[i] += ax * v
            elif p.kind == "stretch":
                for i in self._str[k]:
                    ls[i] += v
        c, s = np.cos(g_yaw), np.sin(g_yaw)
        Ry = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
        G = _affine(np.exp(g_scale) * Ry, g_t)
        A = np.zeros((n, 4, 4))
        for i in self.order:
            L = rotation_matrix(rv[i]) if rv[i].any() else np.eye(3)
            if ls[i] != 0.0 and self.dirs[i] is not None:
                d = self.dirs[i]
                L = L @ (np.eye(3) + (np.exp(ls[i]) - 1.0) * np.outer(d, d))
            M = _about(skel.joints[i], L)
            p = skel.fit_parent(i)
            A[i] = (A[p] if p >= 0 else G) @ M
        return A

    def regularization(self, x: np.ndarray) -> np.ndarray:
        return np.array([(float(x[k]) - p.prior) / p.sigma for k, p in enumerate(self.spec)])


def default_fit_spec(rot_sigma: float = 0.6, stretch_sigma: float = 0.25) -> list[ParamSpec]:
    """既定の合わせ込みパラメーター（31 個）。

    全体（拡大・y 回転・平行移動）、背骨の前後/左右の曲げと胴の長さ、首の長さ、鎖骨の上下と肩幅、
    上腕・前腕・太もも・すね の 2 軸の振り（左右別）、各部の長さ（左右共通）。
    """
    S: list[ParamSpec] = [
        ParamSpec("scale", sigma=0.15, label="global scale(log)"),
        ParamSpec("yaw", sigma=0.35, label="global yaw"),
        ParamSpec("tx", sigma=1.0, label="global tx"),
        ParamSpec("ty", sigma=1.0, label="global ty"),
        ParamSpec("tz", sigma=1.0, label="global tz"),
        ParamSpec("rot", ("Bip01 Spine",), 0, sigma=rot_sigma, label="spine bend x"),
        ParamSpec("rot", ("Bip01 Spine",), 2, sigma=rot_sigma, label="spine bend z"),
        ParamSpec("stretch", ("Bip01 Spine", "Bip01 Spine1"), sigma=stretch_sigma, label="torso length(log)"),
        ParamSpec("stretch", ("Bip01 Neck",), sigma=stretch_sigma, label="neck length(log)"),
        ParamSpec("stretch", ("Bip01 L Clavicle", "Bip01 R Clavicle"), sigma=stretch_sigma, label="shoulder width(log)"),
        ParamSpec("stretch", ("Bip01 L UpperArm", "Bip01 R UpperArm"), sigma=stretch_sigma, label="upper arm length(log)"),
        ParamSpec("stretch", ("Bip01 L Forearm", "Bip01 R Forearm", "Bip01 L ForeTwist", "Bip01 R ForeTwist"),
                  sigma=stretch_sigma, label="forearm length(log)"),
        ParamSpec("stretch", ("Bip01 L Thigh", "Bip01 R Thigh"), sigma=stretch_sigma, label="thigh length(log)"),
        ParamSpec("stretch", ("Bip01 L Calf", "Bip01 R Calf"), sigma=stretch_sigma, label="calf length(log)"),
    ]
    for side in SIDES:
        S.append(ParamSpec("rot", (f"Bip01 {side} Clavicle",), 2, sigma=rot_sigma * 0.5, label=f"{side} clavicle z"))
        for bone, label in (("UpperArm", "upper arm"), ("Forearm", "forearm"), ("Thigh", "thigh"), ("Calf", "calf")):
            bones = (f"Bip01 {side} {bone}",)
            if bone == "Forearm":
                bones = bones + (f"Bip01 {side} ForeTwist",)
            S.append(ParamSpec("swing", bones, 0, sigma=rot_sigma, label=f"{side} {label} swing0"))
            S.append(ParamSpec("swing", bones, 1, sigma=rot_sigma, label=f"{side} {label} swing1"))
    return S


def test_pose_spec() -> tuple[list[ParamSpec], dict[str, float]]:
    """変形確認用の試験姿勢（腕を下ろして肘を曲げ、片脚を前へ出す）。値は度 → rad。"""
    spec = [ParamSpec("rot", (f"Bip01 {s} UpperArm",), 2, label=f"{s} arm z") for s in SIDES]
    spec += [ParamSpec("rot", (f"Bip01 {s} Forearm", f"Bip01 {s} ForeTwist"), 0, label=f"{s} elbow x") for s in SIDES]
    spec += [ParamSpec("rot", ("Bip01 L Thigh",), 0, label="L thigh x"), ParamSpec("rot", ("Bip01 L Calf",), 0, label="L calf x"),
             ParamSpec("rot", ("Bip01 R Thigh",), 0, label="R thigh x"), ParamSpec("rot", ("Bip01 Spine",), 1, label="spine twist")]
    # 左腕は +x 側にあるので z 軸回りの負の回転で下がる（右腕は正）。x 軸回りの負の回転で前（+z）へ出る
    deg = {"L arm z": -30.0, "R arm z": 30.0, "L elbow x": -45.0, "R elbow x": -45.0,
           "L thigh x": -35.0, "L calf x": 40.0, "R thigh x": 15.0, "spine twist": 15.0}
    return spec, {k: np.radians(v) for k, v in deg.items()}
