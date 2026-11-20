"""変換の設定。CLI の引数と JSON（``--config``）から作る。"""
from __future__ import annotations

import json
import math
import numbers
from dataclasses import asdict, dataclass, field, fields

import numpy as np

from ._i18n import tr

__all__ = ["AxisConfig", "ConvertConfig", "parse_axis"]

_AXES = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0), "+z": (0, 0, 1), "-z": (0, 0, -1)}


def parse_axis(s: str) -> np.ndarray:
    key = s.strip().lower()
    if key in ("x", "y", "z"):
        key = "+" + key
    if key not in _AXES:
        raise ValueError(tr(f"軸の指定が不正: {s!r}（+x -x +y -y +z -z のいずれか）",
                            f"Invalid axis: {s!r} (one of +x -x +y -y +z -z)"))
    return np.array(_AXES[key], dtype=np.float64)


@dataclass
class AxisConfig:
    """入力モデルの向き → Grim Dawn の向き。

    * ``source_up`` / ``source_front``: 入力モデルの上方向・正面方向（TRELLIS の GLB は +y / +z）
    * ``target_front``: Grim Dawn のキャラクターの正面方向。骨格（つま先が +z、左手が +x）から +z と推定。
      試作MOD 2 のゲーム内確認で -z と分かった場合は ``-z`` にする（y 軸回りに 180° 回る）
    * ``yaw_deg``: 追加の y 軸回転（度。正面が斜めの入力用）
    * ``mirror_x``: 左右を鏡像にする（入力の左右が逆のとき。巻き順も自動で直す）
    """

    source_up: str = "+y"
    source_front: str = "+z"
    target_front: str = "+z"
    yaw_deg: float = 0.0
    mirror_x: bool = False

    def matrix(self) -> np.ndarray:
        """入力座標 → Grim Dawn 座標の 3×3 行列（列ベクトル規約）。"""
        up = parse_axis(self.source_up)
        fr = parse_axis(self.source_front)
        if abs(float(np.dot(up, fr))) > 1e-9:
            raise ValueError(tr("source_up と source_front が直交していない",
                                "source_up and source_front are not perpendicular"))
        tf = parse_axis(self.target_front)
        if abs(tf[1]) > 0:
            raise ValueError(tr("target_front は水平の軸（±x, ±z）", "target_front must be a horizontal axis (±x, ±z)"))
        # 入力の (right, up, front) → 出力の (right', +y, tf)。right = up × front（右手系で front を向いたときの左手側）
        src = np.stack([np.cross(up, fr), up, fr], axis=1)
        ty = np.array([0.0, 1.0, 0.0])
        dst = np.stack([np.cross(ty, tf), ty, tf], axis=1)
        M = dst @ src.T
        a = np.radians(self.yaw_deg)
        c, s = np.cos(a), np.sin(a)
        M = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]) @ M
        if self.mirror_x:
            M = np.diag([-1.0, 1.0, 1.0]) @ M
        return M


@dataclass
class ConvertConfig:
    """変換全体の設定（既定値は TRELLIS の出力向け）。"""

    axes: AxisConfig = field(default_factory=AxisConfig)
    # --- MeshProcessor ---
    target_vertices: int = 25_000          # D-009: 目標（自動間引きの目安。最終の .msh の頂点数で判定）
    max_vertices: int = 65_535             # D-009: 絶対上限
    weld_tolerance: float = 1e-6           # 入力単位（TRELLIS は高さ 1.0）に対する相対値 × 高さ
    height_mode: str = "bbox"              # "bbox" = 入力の高さを基準ボディの身長に合わせる / "none" = scale のみ
    scale: float = 0.0                     # >0 なら height_mode を無視してこの倍率（入力単位 → GD 単位）
    # --- RiggingProcessor ---
    fit: str = "pose"                      # "pose" = 姿勢・比率の合わせ込み / "none" = 拡大と平行移動のみ
    fit_samples_body: int = 2500
    fit_samples_model: int = 4000
    fit_max_nfev: int = 60
    fit_leg_separation: bool = True        # 左右の膝・足首・つま先が重ならないよう合わせ込みに下限を設ける（B-001）
    fit_seed: int = 12345                  # 合わせ込みで表面の点を選ぶ乱数の種（ばらつきの評価用。通常は変えない）
    max_influences: int = 4
    transfer_max_distance: float = 0.06    # 身長に対する比。これより遠い頂点は補間（inpainting）でウェイトを決める
    transfer_max_angle: float = 60.0       # 度。法線がこれ以上ずれる頂点も補間で決める
    smooth_iterations: int = 3
    island_min_vertices: int = 50          # これより小さい「確かな頂点の島」は捨てて補間に回す
    island_min_fraction: float = 0.005     # 同上（連結成分の頂点数に対する比）
    leg_to_pelvis: bool = True             # 体から離れた補間部分の脚ウェイトを骨盤へ移す（尻尾・布・装飾用）
    leg_to_pelvis_start: float = 0.06      # 身長比。確かな頂点からの表面距離がこれ以上で移し始める
    leg_to_pelvis_end: float = 0.18        # 身長比。これ以上で全部移す
    head_rigid: bool = True                # 補間部分で主に頭の頂点（角・兜飾り・髪）を頭 100% に寄せる
    limb_gating: bool = True               # 四肢の取り違え（そろえた脚の左右逆、手と太もも）を表面距離で消す（B-001）
    ground_snap: str = "legs"              # 足の一番低い点を y=0 に（最大 ±0.15）。legs = 膝から下だけ / all = 全体 / none
    # --- TextureProcessor ---
    texture_max_size: int = 2048
    texture_min_size: int = 256
    texture_format: str = "dxt1"           # TextureCompiler の -format（小文字のみ有効）
    bake_size: int = 0                     # 焼き直しのテクスチャの大きさ（0 = 元と同じか 1024）
    padding_px: int = 8
    # --- GrimDawnExporter ---
    namespace: str = "aiskin3"             # Items.arc 内のフォルダー名
    bump_texture: str = "System/textures/flatnml.tex"
    genders: tuple[str, ...] = ("m", "f")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["genders"] = list(self.genders)
        return d

    def validate(self) -> None:
        """列挙値と数の範囲を確かめる（誤りは ValueError）。綴りの誤りが黙って別の扱いになったり、重い処理の
        後で KeyError になったりしないよう、変換の最初（:func:`gdconvert.pipeline.convert`）と ``--config`` の
        読み込みで呼ぶ。"""
        def _int(v):
            return isinstance(v, int) and not isinstance(v, bool)
        g = self.genders
        if (not isinstance(g, (tuple, list)) or not g or any(x not in ("m", "f") for x in g)
                or len(set(g)) != len(g)):
            raise ValueError(tr(f"genders は m / f（1 つ以上、重複なし）: {g!r}",
                                f"genders must be m / f (at least one, no duplicates): {g!r}"))
        for key, allowed in (("height_mode", ("bbox", "none")), ("fit", ("pose", "none")),
                             ("ground_snap", ("legs", "all", "none", ""))):
            if getattr(self, key) not in allowed:
                raise ValueError(tr(f"{key} は {' / '.join(a for a in allowed if a)}: {getattr(self, key)!r}",
                                    f"{key} must be {' / '.join(a for a in allowed if a)}: {getattr(self, key)!r}"))
        if not _int(self.max_vertices) or not 1 <= self.max_vertices <= 65535:
            raise ValueError(tr(f"max_vertices は 1〜65535 の整数: {self.max_vertices!r}",
                                f"max_vertices must be an integer from 1 to 65535: {self.max_vertices!r}"))
        if not _int(self.target_vertices) or not 1 <= self.target_vertices <= self.max_vertices:
            raise ValueError(tr(f"target_vertices は 1〜{self.max_vertices} の整数: {self.target_vertices!r}",
                                f"target_vertices must be an integer from 1 to {self.max_vertices}: "
                                f"{self.target_vertices!r}"))
        if not _int(self.max_influences) or not 1 <= self.max_influences <= 4:
            raise ValueError(tr(f"max_influences は 1〜4 の整数: {self.max_influences!r}",
                                f"max_influences must be an integer from 1 to 4: {self.max_influences!r}"))
        # テクスチャの各辺は 4 以上（2026-11-04 の利用者の決定、R04A-01）。DXT は 4x4 画素のブロックで、
        # TextureCompiler は 1x1 の画像をミップ数 2 の .tex にし、modbuilder の検査（assets.check_tex）が拒んで
        # MOD を作れない。上限と下限の両方を断る（下限を黙って 4 に丸めると、指定と違う大きさになる。
        # 2 の累乗でない値を断るのと同じ方針。R04A-03）
        if not _int(self.texture_min_size) or self.texture_min_size < 4:
            raise ValueError(tr(f"texture_min_size は 4 以上の整数（.tex の各辺は 4 画素以上）: {self.texture_min_size!r}",
                                f"texture_min_size must be an integer of 4 or more (each side of a .tex must be at "
                                f"least 4 pixels): {self.texture_min_size!r}"))
        # 上限が下限より小さくてもよい（--texture-max 128 など）。そのときは texture.choose_size と
        # meshproc の焼き付けの大きさが上限に合わせる（点検の回帰 D1_lib-01）
        if not _int(self.texture_max_size) or self.texture_max_size < 4:
            raise ValueError(tr(f"texture_max_size は 4 以上の整数（.tex の各辺は 4 画素以上）: {self.texture_max_size!r}",
                                f"texture_max_size must be an integer of 4 or more (each side of a .tex must be at "
                                f"least 4 pixels): {self.texture_max_size!r}"))
        # .tex（TextureCompiler）は各辺 2 の累乗だけ。黙って丸めず、開始前に止める（点検 R04A-03）
        for key in ("texture_min_size", "texture_max_size"):
            v = getattr(self, key)
            if v & (v - 1):
                raise ValueError(tr(f"{key} は 2 の累乗（例 512 / 1024 / 2048）: {v!r}",
                                    f"{key} must be a power of 2 (e.g. 512 / 1024 / 2048): {v!r}"))
        # 軸と数・真偽の値も開始前に確かめる（軸の誤りは工程 2 で GLB を読んだ後のトレースバックに、yaw の NaN は
        # 「NaN の面だけ」という入力のせいにする文に、mirror_x の "false" は黙って左右反転に、weld_tolerance の
        # 文字列は工程 3 の TypeError になっていた。2026-10-27 点検 R02B-06・R02A-06）
        def _num(v):
            return isinstance(v, numbers.Real) and not isinstance(v, bool) and math.isfinite(v)
        ax = self.axes
        if not isinstance(ax, AxisConfig):
            raise ValueError(tr(f"axes が不正: {ax!r}", f"Invalid axes: {ax!r}"))
        for key in ("source_up", "source_front", "target_front"):
            if not isinstance(getattr(ax, key), str):
                raise ValueError(tr(f"axes.{key} は軸の文字列（+x -x +y -y +z -z のいずれか）: {getattr(ax, key)!r}",
                                    f"axes.{key} must be an axis string (one of +x -x +y -y +z -z): "
                                    f"{getattr(ax, key)!r}"))
        if not _num(ax.yaw_deg):
            raise ValueError(tr(f"axes.yaw_deg は有限の数（度）: {ax.yaw_deg!r}",
                                f"axes.yaw_deg must be a finite number (degrees): {ax.yaw_deg!r}"))
        if not isinstance(ax.mirror_x, bool):
            raise ValueError(tr(f"axes.mirror_x は true / false: {ax.mirror_x!r}",
                                f"axes.mirror_x must be true / false: {ax.mirror_x!r}"))
        ax.matrix()         # 不正な軸・直交しない上と正面・正面が ±y はここで ValueError
        for key in ("weld_tolerance", "scale", "transfer_max_distance", "transfer_max_angle", "island_min_fraction",
                    "leg_to_pelvis_start", "leg_to_pelvis_end"):
            v = getattr(self, key)
            if not _num(v) or v < 0:
                raise ValueError(tr(f"{key} は 0 以上の有限の数: {v!r}", f"{key} must be a finite number of 0 or more: {v!r}"))
        for key, lo in (("bake_size", 0), ("padding_px", 0), ("fit_samples_body", 1), ("fit_samples_model", 1),
                        ("fit_max_nfev", 1), ("fit_seed", 0), ("smooth_iterations", 0), ("island_min_vertices", 0)):
            v = getattr(self, key)
            if not _int(v) or v < lo:
                raise ValueError(tr(f"{key} は {lo} 以上の整数: {v!r}", f"{key} must be an integer of {lo} or more: {v!r}"))
        for key in ("fit_leg_separation", "leg_to_pelvis", "head_rigid", "limb_gating"):
            if not isinstance(getattr(self, key), bool):
                raise ValueError(tr(f"{key} は true / false: {getattr(self, key)!r}",
                                    f"{key} must be true / false: {getattr(self, key)!r}"))
        # TextureCompiler の -format は dxt1 / dxt3 / dxt5 だけ（texture.compile_tex は小文字にして渡す）
        if not isinstance(self.texture_format, str) or self.texture_format.lower() not in ("dxt1", "dxt3", "dxt5"):
            raise ValueError(tr(f"texture_format は dxt1 / dxt3 / dxt5: {self.texture_format!r}",
                                f"texture_format must be dxt1 / dxt3 / dxt5: {self.texture_format!r}"))
        if not isinstance(self.bump_texture, str):
            raise ValueError(tr(f"bump_texture は文字列: {self.bump_texture!r}",
                                f"bump_texture must be a string: {self.bump_texture!r}"))

    @classmethod
    def from_dict(cls, d: dict) -> "ConvertConfig":
        # 最上位がオブジェクトでない・axes がオブジェクトでない・axes の不明な項目は、TypeError ではなく
        # ValueError にする（CLI が「エラー: …」と終了コード 2 で止められるように。点検 R02B-06）
        if not isinstance(d, dict):
            raise ValueError(tr(f"設定はオブジェクト（{{...}}）: {type(d).__name__}",
                                f"The settings must be an object ({{...}}): {type(d).__name__}"))
        d = dict(d)
        ad = d.pop("axes", {})
        if not isinstance(ad, dict):
            raise ValueError(tr(f"axes はオブジェクト（{{...}}）: {ad!r}", f"axes must be an object ({{...}}): {ad!r}"))
        bad_axes = set(ad) - {f.name for f in fields(AxisConfig)}
        if bad_axes:
            raise ValueError(tr(f"axes の不明な項目: {sorted(bad_axes)}", f"Unknown axes settings: {sorted(bad_axes)}"))
        axes = AxisConfig(**ad)
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(tr(f"不明な設定項目: {sorted(unknown)}", f"Unknown settings: {sorted(unknown)}"))
        if "genders" in d:
            if isinstance(d["genders"], str):      # "m,f" を 1 文字ずつに分けない
                raise ValueError(tr(f"genders は配列（例 [\"m\", \"f\"]）: {d['genders']!r}",
                                    f"genders must be an array (e.g. [\"m\", \"f\"]): {d['genders']!r}"))
            d["genders"] = tuple(d["genders"])
        cfg = cls(axes=axes, **d)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str) -> "ConvertConfig":
        # BOM 付きの UTF-8（Windows PowerShell 5.1 の Set-Content -Encoding UTF8 など）も読む
        with open(path, encoding="utf-8-sig") as f:
            return cls.from_dict(json.load(f))
