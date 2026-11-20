"""基準ボディと出力用ボーン表の元になる公式 .msh の準備と読み込み。

公式ファイルは配布物に含めず、ユーザーのゲームから取り出す。取り出しはゲームの ``resources/<Name>.arc`` を
:class:`modbuilder.arc.ArcFile` で読み取り専用で直接読む（.arc の更新日時は変わらない）。ArchiveTool は使わない
（読み出しだけでも .arc の更新日時を変えるので約 0.95 GB のコピーが要り、``-extract`` は出力先のパスを cp932 の
バイト単位で小文字化するので、利用者フォルダー名に 山・ス・ソ・表 などを含む PC では失敗・停止する）。
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass

import numpy as np

from gdmesh import msh

from ._i18n import tr
from .skeleton import Skeleton, lbs, retarget_transforms

__all__ = ["REF_FILES", "DEFAULT_GD", "prepare_refs", "missing_refs", "unreadable_refs", "ref_problems", "ref_path",
           "RefsCorruptError", "RefsUnreadableError", "SourceBody", "GenderRefs", "load_refs"]

DEFAULT_GD = r"C:\Program Files (x86)\Steam\steamapps\common\Grim Dawn"

# 役割 → ゲーム内パス
REF_FILES = {
    # 男性: ウェイトと形の元（防具なしの全身。27 本のボーンにウェイト）
    "body_m": "creatures/pc/hero_00_body_noarmor.msh",
    # 男性: 出力のボーン表とヒットボックスの元（頭スロットに入れて表示されることを試作MOD 1 で確認済みの公式胴メッシュ）
    "template_m": "items/geartorso/torso_default_000-01_m.msh",
    # 女性: 出力のボーン表とヒットボックスの元（女性の全身メッシュ。胴はローブに覆われていて素体が無いので、
    #       ウェイトの元には使わず、男性の素体を女性の骨格に写して使う）
    "template_f": "creatures/pc/heroine_00_body.msh",
    # 出力のボーン名がプレイヤー骨格に含まれるかの照合用（プレイヤーの基本メッシュ。56 本 / 49 本）
    "player_m": "creatures/pc/hero_00.msh",
    "player_f": "creatures/pc/heroine_00.msh",
    # 調査・比較用（変換には使わない）
    "body_m_armor": "creatures/pc/hero_00_body.msh",
}
REQUIRED = ("body_m", "template_m", "template_f")


class RefsCorruptError(ValueError):
    """基準ファイルはあるが .msh として読めない（途中で切れた・0 で埋まった・別の中身など）。

    ``path`` は refs フォルダーの中のファイル、``rel`` はゲーム内パス（:data:`REF_FILES` の値）。
    :func:`prepare_refs` で取り出し直せば直る。``reason`` は ``corrupt``（:class:`RefsUnreadableError` は
    ``unreadable``）。"""

    reason = "corrupt"

    def __init__(self, path: str, rel: str, detail: str, message: str | None = None):
        super().__init__(message or tr(f"基準ファイルが壊れていて読めない: {path}（{detail}）。ゲームから取り出し直す"
                                       f"（アプリの「ゲームから準備する」、または gdconvert refs）",
                                       f"A reference file is corrupted and cannot be read: {path} ({detail}). Extract it "
                                       f"again from the game (\"Prepare from the game\" in the app, or gdconvert refs)"))
        self.path = path
        self.rel = rel


class RefsUnreadableError(RefsCorruptError):
    """基準ファイルはあるが開けない・読めない（OSError。ほかのソフト（ウイルス対策・バックアップ・同期）がロックして
    いる・アクセス権が無い・ディスクの読み取りエラーなど）。中身が壊れているとは限らないので、取り出し直しでは直らない
    ことがある。``errno`` は元の OSError の番号（無ければ None）。

    :class:`RefsCorruptError` の子なので、それを捕まえる呼び出し元（アプリの REFS_MISSING）はそのまま分けられる。
    2026-11-04 点検 D1_lib-01（以前は PermissionError がそのまま出て、環境の確認の全体と「ゲームから準備する」が止まった）。"""

    reason = "unreadable"

    def __init__(self, path: str, rel: str, error: OSError):
        detail = f"{type(error).__name__}: {error.strerror or error}"
        super().__init__(path, rel, detail, tr(
            f"基準ファイルを開けない・読めない: {path}（{detail}）。ほかのソフト（ウイルス対策・バックアップ・同期など）が"
            f"開いていないか、アクセス権を確かめてからやり直す",
            f"A reference file cannot be opened or read: {path} ({detail}). Check that another program (antivirus, "
            f"backup, sync, etc.) does not have it open and that you have permission to read it, then try again"))
        self.errno = error.errno


def _read_ref(refs_dir: str, role: str) -> msh.MshFile:
    """基準ファイルを読む。壊れている・頂点・三角形・ボーンのチャンクが無ければ :class:`RefsCorruptError`、開けない・
    読めなければ :class:`RefsUnreadableError`（どちらもどのファイルかと直し方が分かる文）。無ければ FileNotFoundError。"""
    p = ref_path(refs_dir, role)
    try:
        m = msh.read(p)
    except (msh.MshError, ValueError, IndexError, EOFError, struct.error) as e:      # 構造の不正（切れ端・0 埋め）
        raise RefsCorruptError(p, REF_FILES[role], f"{type(e).__name__}: {e}") from None
    except FileNotFoundError:
        raise
    except OSError as e:                    # ロック・アクセス権・読み取りエラー（点検 D1_lib-01）
        raise RefsUnreadableError(p, REF_FILES[role], e) from None
    lack = [k for k in ("vertices", "triangles", "bones") if getattr(m, k) is None]
    if lack:                                # 読めないチャンクは RawChunk になる（非厳密の読み込み）
        raise RefsCorruptError(p, REF_FILES[role], tr(f"読めないチャンク: {lack}", f"unreadable chunks: {lack}"))
    return m


def player_bone_names(refs_dir: str) -> dict[str, set[str]]:
    """性別ごとのプレイヤー骨格のボーン名（ファイルが無ければその性別は省く）。"""
    out = {}
    for g in ("m", "f"):
        p = ref_path(refs_dir, f"player_{g}")
        if os.path.isfile(p):
            out[g] = {b.name for b in _read_ref(refs_dir, f"player_{g}").bones.bones}
    return out


def ref_path(refs_dir: str, role: str) -> str:
    return os.path.join(refs_dir, REF_FILES[role].replace("/", os.sep))


def missing_refs(refs_dir: str, roles=REQUIRED) -> list[str]:
    return [REF_FILES[r] for r in roles if not os.path.isfile(ref_path(refs_dir, r))]


def ref_problems(refs_dir: str, roles=REQUIRED) -> dict[str, str]:
    """ファイルはあるが基準として読めないもの → {ゲーム内パス: 理由}。理由は ``corrupt``（途中で切れた・0 で埋まった
    など。取り出し直せば直る）か ``unreadable``（開けない・読めない。ほかのソフトのロック・アクセス権など）。
    例外は投げない（点検 D1_lib-01。読み取りの失敗で環境の確認の全体を止めない）。無いファイルは :func:`missing_refs`。"""
    out = {}
    for r in roles:
        if os.path.isfile(ref_path(refs_dir, r)):
            try:
                _read_ref(refs_dir, r)
            except RefsCorruptError as e:   # RefsUnreadableError も（reason で分ける）
                out[REF_FILES[r]] = e.reason
            except FileNotFoundError:       # 確かめた直後に消えた。無いファイルとして missing_refs に任せる
                pass
    return out


def unreadable_refs(refs_dir: str, roles=REQUIRED) -> list[str]:
    """ファイルはあるが基準として読めない（:func:`load_refs` が :class:`RefsCorruptError` になる）ゲーム内パスの一覧。
    途中で切れた・0 で埋まったファイルなど（手作業のコピー、ディスク・ウイルス対策ソフトの影響、電源断）と、開けない・
    読めないファイル（:class:`RefsUnreadableError`。理由で分けるなら :func:`ref_problems`）。例外は投げない。"""
    return list(ref_problems(refs_dir, roles))


def prepare_refs(refs_dir: str, gd: str = DEFAULT_GD, roles=tuple(REF_FILES), log=print) -> list[str]:
    """足りない公式 .msh を、ゲームの ``<gd>/resources/<Name>.arc``（本編）から直接読んで
    ``refs_dir/<arc名>/<entry>`` に書く（.arc は読み取り専用で開くだけ。コピーも ArchiveTool も使わない）。

    書き込みは同じフォルダーの一時ファイルから ``os.replace`` で置くので、途中で止まっても欠けたファイルは残らない
    （:func:`missing_refs` はファイルの有無だけで判断するため）。ログは足りないファイル 1 件ごとに「取り出し: <path>」
    （英語の表示では「Extracted: <path>」）。あるが壊れているファイル（:func:`ref_problems` の ``corrupt``。途中で切れた・
    0 で埋まったなど）も、.arc の中身と違えば取り出し直す（同じなら取り出し直しても変わらないので触らない）。そのときは前に
    「壊れている基準ファイルを取り出し直す: <path>」、置いた後に「壊れている基準ファイルを置き換えた: <path>」を出す（英語は
    「Re-extracting a corrupted reference file: 」「Replaced the corrupted reference file: 」）。アプリの task_refs は
    「取り出し: 」と「壊れている基準ファイルを置き換えた: 」の行を数え、足りないファイルと壊れたファイルの数の合計を分母に
    進捗を出す（2026-11-04 退行の確認 2 回目 B4）。前の「…を取り出し直す: 」の行は数えないので「取り出し: 」で始めない
    （数えると 1 件を 2 回数える。点検 D1_lib-02）。この 2 つの行の文言を変えるときは worker_tasks.task_refs の done_marks も
    合わせる。開けない・読めないファイル（``unreadable``。ほかのソフトのロックなど）は中身を比べられないので、「注意: 」の行を
    出して飛ばす（例外にしない。点検 D1_lib-01）。

    例外: .arc が無い → FileNotFoundError、項目が無い → KeyError、壊れている（大きさ・Adler32 の不一致）→ ValueError、
    refs のファイルを置けない（ほかのソフトが開いている・アクセス権）→ OSError（直し方の案内付きの文）。
    戻り値: 取り出したゲーム内パスの一覧。
    """
    need = missing_refs(refs_dir, roles)
    problems = ref_problems(refs_dir, roles)
    broken = [rel for rel, why in problems.items() if why == "corrupt"]
    for rel, why in problems.items():
        if why == "unreadable":
            target = os.path.join(refs_dir, rel.replace("/", os.sep))
            log(tr(f"注意: 基準ファイルを読めないので取り出し直さない: {target}（ほかのソフト（ウイルス対策・バックアップ・"
                   f"同期など）が開いていないか、アクセス権を確かめてからやり直す）",
                   f"Warning: cannot read a reference file, so it is not extracted again: {target} (check that "
                   f"another program (antivirus, backup, sync, etc.) does not have it open and that you have permission "
                   f"to read it, then try again)"))
    if not need and not broken:
        return []
    from modbuilder.arc import ArcFile      # lz4 と modbuilder は取り出しのときだけ要る（load_refs・変換は使わない）
    arcs: dict[str, ArcFile] = {}
    done = []
    for rel in need + broken:
        arcname, entry = rel.split("/", 1)
        arc = arcs.get(arcname)
        if arc is None:                     # .arc は名前ごとに 1 回だけ開く
            src = os.path.join(gd, "resources", arcname.capitalize() + ".arc")
            if not os.path.isfile(src):
                raise FileNotFoundError(tr(f"ゲームの .arc が見つからない: {src}（--gd でゲームのフォルダーを指定）",
                                           f"The game's .arc was not found: {src} (specify the game folder with --gd)"))
            arc = arcs[arcname] = ArcFile(src)
        data = arc.read(entry)
        target = os.path.join(refs_dir, arcname, entry.replace("/", os.sep))
        if rel in broken:
            try:
                with open(target, "rb") as f:
                    same = f.read() == data
            except FileNotFoundError:
                same = False                # 確かめた後で消えた。足りないファイルとして書く
            except OSError as e:            # 確かめた後で読めなくなった（ロックなど）。飛ばす（点検 D1_lib-01）
                log(tr(f"注意: 基準ファイルを読めないので取り出し直さない: {target}（{e}）",
                       f"Warning: cannot read a reference file, so it is not extracted again: {target} ({e})"))
                continue
            if same:
                continue
            log(tr(f"壊れている基準ファイルを取り出し直す: {target}", f"Re-extracting a corrupted reference file: {target}"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f"{target}.{os.getpid()}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        except OSError as e:
            # 置き換え先をほかのソフトが開いている・アクセス権が無いなど。どのファイルかと直し方が分かる文にする
            # （errno から PermissionError などの同じ種類の例外になる。点検 D1_lib-01）
            raise OSError(e.errno, tr(f"基準ファイルを書き込めない: {target}（{e.strerror or e}）。ほかのソフト"
                                      f"（ウイルス対策・バックアップ・同期など）を閉じるか、アクセス権を確かめてからやり直す",
                                      f"Cannot write a reference file: {target} ({e.strerror or e}). Close other programs "
                                      f"(antivirus, backup, sync, etc.) or check your permissions, then try again")) from e
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        if rel in broken:                   # task_refs はこの行も数える（分母は足りないファイルと壊れたファイルの数。B4）。
            # 文言を変えるときは worker_tasks.task_refs の done_marks も直す
            log(tr(f"壊れている基準ファイルを置き換えた: {target}", f"Replaced the corrupted reference file: {target}"))
        else:
            log(tr(f"取り出し: {target}", f"Extracted: {target}"))
        done.append(rel)
    return done


# ----------------------------------------------------------------------
# 基準ボディ
# ----------------------------------------------------------------------
def global_bone_indices(m: msh.MshFile) -> np.ndarray:
    """頂点のボーン番号（セクションのパレット添字）をボーン表のグローバル番号に直す。(n,4)、未使用は 0。"""
    v = m.vertices
    out = np.zeros((v.count, 4), dtype=np.int64)
    tri = m.triangles.indices
    loc = v.bone_indices.astype(np.int64)
    for s in m.triangles.sections:
        idx = np.unique(tri[s.start:s.start + s.count].reshape(-1))
        pal = np.array(list(s.bones) + [0], dtype=np.int64)
        li = loc[idx]
        li = np.where((li >= len(s.bones)), len(s.bones), li)
        out[idx] = pal[li]
    w = v.weights
    out[w <= 0] = 0
    return out


@dataclass
class SourceBody:
    """ウェイト転写の元になる全身メッシュ。faces は反時計回り（外向き法線が右手系）。"""

    positions: np.ndarray          # (n, 3)
    faces: np.ndarray              # (m, 3)
    bone_idx: np.ndarray           # (n, 4) skeleton のボーン番号
    weights: np.ndarray            # (n, 4)
    skeleton: Skeleton
    name: str = ""

    @classmethod
    def from_msh(cls, m: msh.MshFile, name: str = "", sections: list[int] | None = None) -> "SourceBody":
        sk = Skeleton.from_msh(m, name)
        v = m.vertices
        tri = m.triangles.indices.astype(np.int64)
        if sections is not None:
            parts = [tri[s.start:s.start + s.count] for k, s in enumerate(m.triangles.sections) if k in sections]
            tri = np.vstack(parts)
        faces = tri[:, [0, 2, 1]]                      # .msh は時計回り → 反時計回り
        gbi = global_bone_indices(m)
        w = v.weights.astype(np.float64)
        w = w / np.maximum(w.sum(axis=1, keepdims=True), 1e-12)
        used = np.unique(faces)
        remap = np.full(v.count, -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        return cls(v.positions.astype(np.float64)[used], remap[faces], gbi[used], w[used], sk, name)

    def dense_weights(self) -> np.ndarray:
        W = np.zeros((len(self.positions), len(self.skeleton)))
        rows = np.repeat(np.arange(len(self.positions)), 4)
        np.add.at(W, (rows, self.bone_idx.reshape(-1)), self.weights.reshape(-1))
        return W

    def retarget(self, dst: Skeleton, name: str = "") -> "SourceBody":
        """自分の骨格のバインド姿勢から dst のバインド姿勢へ形を写し、ウェイトを dst のボーン番号にする。"""
        A = retarget_transforms(self.skeleton, dst)
        pos = lbs(self.positions, self.bone_idx, self.weights, A)
        m = self.skeleton.map_names(dst)
        return SourceBody(pos, self.faces.copy(), m[self.bone_idx], self.weights.copy(), dst, name or self.name)

    def to_skeleton_indices(self, dst: Skeleton) -> "SourceBody":
        """形はそのままで、ボーン番号だけ dst（同じ骨格で並びが違うボーン表）に付け替える。"""
        m = self.skeleton.map_names(dst)
        return SourceBody(self.positions.copy(), self.faces.copy(), m[self.bone_idx], self.weights.copy(), dst, self.name)

    @property
    def height(self) -> float:
        return float(self.positions[:, 1].max() - min(0.0, float(self.positions[:, 1].min())))


@dataclass
class GenderRefs:
    """性別ごとの出力先骨格と、その骨格に合わせた基準ボディ。"""

    gender: str                    # "m" / "f"
    skeleton: Skeleton             # 出力 .msh のボーン表（template の並び）
    body: SourceBody               # skeleton のボーン番号でウェイトを持つ基準ボディ
    template: msh.MshFile          # ボーン表・ヒットボックス・マテリアル値の元
    template_path: str


def load_refs(refs_dir: str) -> dict[str, GenderRefs]:
    """基準ファイルを読む。無ければ FileNotFoundError、あるが読めなければ :class:`RefsCorruptError`。"""
    miss = missing_refs(refs_dir)
    if miss:
        raise FileNotFoundError(tr(f"基準ファイルがない（gdconvert refs で取り出す）: {miss}",
                                   f"Reference files are missing (extract them with gdconvert refs): {miss}"))
    body_m_msh = _read_ref(refs_dir, "body_m")
    tm_path = ref_path(refs_dir, "template_m")
    tf_path = ref_path(refs_dir, "template_f")
    tm = _read_ref(refs_dir, "template_m")
    tf = _read_ref(refs_dir, "template_f")
    body_m = SourceBody.from_msh(body_m_msh, REF_FILES["body_m"])
    sk_m = Skeleton.from_msh(tm, REF_FILES["template_m"])
    sk_f = Skeleton.from_msh(tf, REF_FILES["template_f"])
    # 男性: 素体と template は同じバインド姿勢（関節位置の差 0 を確認済み）。番号だけ付け替える
    diff = max(float(np.abs(body_m.skeleton.joints[i] - sk_m.joints[sk_m.index(n)]).max())
               for i, n in enumerate(body_m.skeleton.names) if sk_m.has(n))
    if diff > 1e-4:
        body_on_m = body_m.retarget(sk_m)
    else:
        body_on_m = body_m.to_skeleton_indices(sk_m)
    body_on_f = body_m.retarget(sk_f, REF_FILES["body_m"] + " → female skeleton")
    return {
        "m": GenderRefs("m", sk_m, body_on_m, tm, tm_path),
        "f": GenderRefs("f", sk_f, body_on_f, tf, tf_path),
    }
