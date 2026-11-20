"""Grim Dawn ``.msh`` メッシュファイルのリーダー / ライター。

ファイル構造（``docs/msh_format.md`` 参照）::

    "MSH" + u8 version(3)
    繰り返し: [u32 chunk_id][u32 payload_size][payload]

各チャンクは型付きクラスに解釈され、``to_bytes()`` で元と同じバイト列に戻せる。
解釈できないチャンクは :class:`RawChunk` として生のまま保持される。

使い方::

    from gdmesh import msh
    m = msh.read("head_004a_01.msh")
    print(m.vertices.count, m.triangles.count, [b.name for b in m.bones.bones])
    msh.write(m, "copy.msh")          # バイト一致で書き戻せる
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import BinaryIO, Iterator, Union

import numpy as np

__all__ = [
    "MAGIC", "VERSION",
    "CHUNK_SOURCE", "CHUNK_UNKNOWN2", "CHUNK_ATTACH", "CHUNK_VERTICES", "CHUNK_TRIANGLES",
    "CHUNK_BONES", "CHUNK_MATERIALS", "CHUNK_HITBOXES", "CHUNK_BBOX", "CHUNK_FLAGS",
    "ATTR_POSITION", "ATTR_NORMAL", "ATTR_TANGENT", "ATTR_BINORMAL", "ATTR_UV",
    "ATTR_WEIGHTS", "ATTR_BONE_INDICES", "ATTR_UV2", "ATTR_COLOR", "ATTR_FORMAT", "ATTR_NAMES",
    "PARAM_STRING", "PARAM_FLOAT2", "PARAM_FLOAT3", "PARAM_FLOAT", "PARAM_FLOAT4",
    "STATIC_LAYOUT", "SKINNED_LAYOUT", "MAX_PALETTE", "MAX_VERTEX_ATTRS", "REQUIRED_CHUNKS",
    "MshError", "Chunk", "RawChunk", "SourceChunk", "FlagsChunk", "BBoxChunk",
    "VertexChunk", "Section", "TriangleChunk", "Bone", "BoneChunk", "Hitbox", "HitboxChunk",
    "MaterialParam", "Material", "MaterialChunk", "AttachChunk", "MshFile",
    "read", "read_bytes", "write", "to_bytes", "parse_chunk",
]

MAGIC = b"MSH"
VERSION = 3

# ---- チャンク ID（確認済み: 本編の Items.arc・Creatures.arc・System.arc の 2,091 ファイル中に現れたもの）----
# それ以外の .arc（本編の Level Art 等と gdx1〜3）には、チャンク 9・11・12 と長いチャンク 13 もある。
# これらは解釈せず RawChunk として生のまま保持する（往復はバイト一致）。
CHUNK_SOURCE = 0      # 元の .mif パス（任意）
CHUNK_UNKNOWN2 = 2    # 2,091 個では 1 ファイルだけ（全 .arc では Level Art を中心に 945 個）。構造未解明（生バイト保持）
CHUNK_ATTACH = 3      # アタッチポイント / CreateEntity のテキスト（任意）
CHUNK_VERTICES = 4
CHUNK_TRIANGLES = 5
CHUNK_BONES = 6
CHUNK_MATERIALS = 7
CHUNK_HITBOXES = 8
CHUNK_BBOX = 10
CHUNK_FLAGS = 13

# ---- 頂点属性 ID ----
ATTR_POSITION = 0
ATTR_NORMAL = 1
ATTR_TANGENT = 2
ATTR_BINORMAL = 3
ATTR_UV = 4
ATTR_WEIGHTS = 5
ATTR_BONE_INDICES = 6
ATTR_UV2 = 7          # float×2。2 つ目の UV と推定（本編の Level Art と gdx1〜3 にある。2,091 個には無い）
ATTR_COLOR = 14       # uint8×4。頂点カラー RGBA と推定

# 属性 ID -> (numpy dtype, 要素数)
ATTR_FORMAT = {
    ATTR_POSITION: ("<f4", 3),
    ATTR_NORMAL: ("<f4", 3),
    ATTR_TANGENT: ("<f4", 3),
    ATTR_BINORMAL: ("<f4", 3),
    ATTR_UV: ("<f4", 2),
    ATTR_WEIGHTS: ("<f4", 4),
    ATTR_BONE_INDICES: ("u1", 4),
    ATTR_UV2: ("<f4", 2),
    ATTR_COLOR: ("u1", 4),
}
ATTR_NAMES = {
    ATTR_POSITION: "position",
    ATTR_NORMAL: "normal",
    ATTR_TANGENT: "tangent",
    ATTR_BINORMAL: "binormal",
    ATTR_UV: "uv",
    ATTR_WEIGHTS: "weights",
    ATTR_BONE_INDICES: "bone_indices",
    ATTR_UV2: "uv2",
    ATTR_COLOR: "color",
}
# ゲーム同梱メッシュで使われている属性順
STATIC_LAYOUT = (ATTR_POSITION, ATTR_UV, ATTR_NORMAL, ATTR_TANGENT, ATTR_BINORMAL)
SKINNED_LAYOUT = (ATTR_POSITION, ATTR_UV, ATTR_NORMAL, ATTR_BONE_INDICES, ATTR_WEIGHTS,
                  ATTR_TANGENT, ATTR_BINORMAL)

# ---- マテリアルパラメータの型 ----
PARAM_STRING = 7      # u32 長さ + 文字列（NUL なし）。テクスチャパスに使われる
PARAM_FLOAT2 = 8      # float×2（scrollRate）
PARAM_FLOAT3 = 9      # float×3（specularColor）
PARAM_FLOAT = 10      # float×1
PARAM_FLOAT4 = 11     # float×4（innerColor・topColor・bottomColor。色と推定。gdx3 にだけある）

# スキンのシェーダー（standardskinned.ssh）のボーン行列の配列は boneMatrix[27]。
# 1 セクションのボーンパレットがこれを超えると、28 本目以降の番号の頂点は配列の外を読む。
MAX_PALETTE = 27

# チャンク 4 の属性数の上限。ゲームの全 .msh の最大は 8（既知の属性は 9 種類）。これを超える頂点チャンクは
# 解釈しない（厳密モードでは MshError、厳密でないモードでは RawChunk のまま保持）
MAX_VERTEX_ATTRS = 16

# 必須チャンク（ゲームの全 .msh で 1 個ずつ、この順に現れる。docs/msh_format.md 1.1 節）と、任意で高々 1 個のチャンク
REQUIRED_CHUNKS = (CHUNK_FLAGS, CHUNK_BBOX, CHUNK_VERTICES, CHUNK_TRIANGLES, CHUNK_BONES, CHUNK_HITBOXES,
                   CHUNK_MATERIALS)
_OPTIONAL_SINGLE_CHUNKS = (CHUNK_SOURCE, CHUNK_ATTACH)

_BONE_NAME_LEN = 32
_BONE_ENTRY_LEN = 88
_HITBOX_ENTRY_LEN = 96


class MshError(ValueError):
    """ファイル構造が想定と異なるときに送出される。"""


def _latin1(text: str, what: str) -> bytes:
    """文字列をファイルに書くバイト列（latin-1）にする。

    latin-1 で表せない文字（漢字など）があるときは、どの値かが分かる :class:`MshError` にする
    （素の UnicodeEncodeError では、どのパラメーターが悪いのか分からないため）。
    """
    try:
        return text.encode("latin-1")
    except UnicodeEncodeError as e:
        raise MshError(f"{what} {text!r}: contains characters that cannot be written (latin-1; use ASCII)") from e


# ======================================================================
# チャンク基底
# ======================================================================
class Chunk:
    """全チャンクの基底。``id`` と ``to_bytes()`` を持つ。"""

    id: int  # サブクラスで定義する

    def to_bytes(self) -> bytes:  # pragma: no cover - サブクラスで実装
        raise NotImplementedError

    @classmethod
    def parse(cls, payload: bytes) -> "Chunk":  # pragma: no cover
        raise NotImplementedError


@dataclass
class RawChunk(Chunk):
    """解釈しない（できない）チャンク。生バイトをそのまま保持する。"""

    id: int
    payload: bytes

    def to_bytes(self) -> bytes:
        return bytes(self.payload)


# ======================================================================
# 0: 元ファイルパス
# ======================================================================
@dataclass
class SourceChunk(Chunk):
    """チャンク 0: ``[u32 len][len バイトの文字列]``。元の ``.mif`` パス。"""

    path: str
    id: int = CHUNK_SOURCE

    @classmethod
    def parse(cls, payload: bytes) -> "SourceChunk":
        n = _u32(payload, 0)
        if 4 + n != len(payload):
            raise MshError(f"chunk 0: length {n} does not match payload {len(payload)}")
        return cls(payload[4:4 + n].decode("latin-1"))

    def to_bytes(self) -> bytes:
        b = _latin1(self.path, "chunk 0 source path")
        return struct.pack("<I", len(b)) + b


# ======================================================================
# 13: フラグ
# ======================================================================
@dataclass
class FlagsChunk(Chunk):
    """チャンク 13: u32×2。2,091 個のサンプルすべてで ``(1, 0)``。意味は未確認。

    本編の Level Art.arc には 26〜46 バイトのチャンク 13 が 19 個ある。これは解釈せず、
    厳密でないモードでは :class:`RawChunk` のまま保持する（厳密モードでは :class:`MshError`）。
    """

    a: int = 1
    b: int = 0
    id: int = CHUNK_FLAGS

    @classmethod
    def parse(cls, payload: bytes) -> "FlagsChunk":
        if len(payload) != 8:
            raise MshError(f"chunk 13: expected 8 bytes, got {len(payload)}")
        a, b = struct.unpack("<II", payload)
        return cls(a, b)

    def to_bytes(self) -> bytes:
        return struct.pack("<II", self.a, self.b)


# ======================================================================
# 10: バウンディングボックス
# ======================================================================
@dataclass
class BBoxChunk(Chunk):
    """チャンク 10: float×6 = 最小 xyz, 最大 xyz（頂点位置の min/max と完全一致）。"""

    min: np.ndarray  # shape (3,), float32
    max: np.ndarray  # shape (3,), float32
    id: int = CHUNK_BBOX

    @classmethod
    def parse(cls, payload: bytes) -> "BBoxChunk":
        if len(payload) != 24:
            raise MshError(f"chunk 10: expected 24 bytes, got {len(payload)}")
        v = np.frombuffer(payload, dtype="<f4").copy()
        return cls(v[:3], v[3:])

    def to_bytes(self) -> bytes:
        return np.ascontiguousarray(self.min, dtype="<f4").tobytes() + \
            np.ascontiguousarray(self.max, dtype="<f4").tobytes()


# ======================================================================
# 4: 頂点
# ======================================================================
def vertex_dtype(attributes: tuple[int, ...] | list[int]) -> np.dtype:
    """属性 ID 列から numpy の構造化 dtype を作る。未知の属性 ID は ``KeyError``、同じ ID の重複は :class:`MshError`。"""
    if len(set(attributes)) != len(attributes):
        raise MshError(f"duplicate vertex attribute in {list(attributes)}")
    fields = []
    for a in attributes:
        base, n = ATTR_FORMAT[a]
        fields.append((ATTR_NAMES[a], base, (n,)))
    return np.dtype(fields)


@dataclass
class VertexChunk(Chunk):
    """チャンク 4: ``[u32 属性数][u32 ストライド][u32 頂点数][u32 属性ID×n][頂点データ]``。

    ``data`` は構造化配列（フィールド名は :data:`ATTR_NAMES`）。属性 ID が未知の場合は
    ``data`` が ``None`` になり ``raw`` に頂点データが生のまま入る。
    """

    attributes: tuple[int, ...]
    stride: int
    data: np.ndarray | None          # 構造化配列 shape (count,)
    raw: bytes | None = None          # 未知レイアウトのときのみ
    id: int = CHUNK_VERTICES

    # -- 生成 --
    @classmethod
    def from_arrays(cls, attributes: tuple[int, ...] | list[int], **arrays: np.ndarray) -> "VertexChunk":
        """``position=..., uv=..., normal=...`` のように属性名で配列を渡して作る。"""
        attributes = tuple(attributes)
        dt = vertex_dtype(attributes)
        names = [ATTR_NAMES[a] for a in attributes]
        missing = [n for n in names if n not in arrays]
        if missing:
            raise ValueError(f"missing vertex arrays: {missing}")
        count = len(arrays[names[0]])
        data = np.zeros(count, dtype=dt)
        for n in names:
            arr = np.asarray(arrays[n])
            if arr.shape != data[n].shape:
                raise ValueError(f"vertex array {n!r}: expected shape {data[n].shape}, got {arr.shape}")
            data[n] = arr
        return cls(attributes, dt.itemsize, data)

    # -- 解析 --
    @classmethod
    def parse(cls, payload: bytes) -> "VertexChunk":
        if len(payload) < 12:
            raise MshError("chunk 4: too short")
        nattr, stride, count = struct.unpack_from("<III", payload, 0)
        # 属性の列を展開する前に数を抑える（巨大な属性数で、小さなファイルが数 GB のメモリーと数十秒を使うため）
        if nattr > MAX_VERTEX_ATTRS:
            raise MshError(f"chunk 4: {nattr} attributes > {MAX_VERTEX_ATTRS}")
        if len(payload) < 12 + 4 * nattr:
            raise MshError("chunk 4: truncated attribute list")
        attrs = struct.unpack_from(f"<{nattr}I", payload, 12)
        body = payload[12 + 4 * nattr:]
        if len(body) != stride * count:
            raise MshError(f"chunk 4: body {len(body)} != stride {stride} * count {count}")
        try:
            dt = vertex_dtype(attrs)
        except KeyError:
            return cls(tuple(attrs), stride, None, raw=bytes(body))
        if dt.itemsize != stride:
            return cls(tuple(attrs), stride, None, raw=bytes(body))
        data = np.frombuffer(body, dtype=dt).copy()
        return cls(tuple(attrs), stride, data)

    def to_bytes(self) -> bytes:
        head = struct.pack("<III", len(self.attributes), self.stride, self.count)
        head += struct.pack(f"<{len(self.attributes)}I", *self.attributes)
        if self.data is None:
            return head + (self.raw or b"")
        return head + np.ascontiguousarray(self.data).tobytes()

    # -- アクセサ --
    @property
    def count(self) -> int:
        if self.data is not None:
            return len(self.data)
        return len(self.raw or b"") // self.stride if self.stride else 0

    def _field(self, name: str) -> np.ndarray | None:
        if self.data is None or name not in self.data.dtype.names:
            return None
        return self.data[name]

    @property
    def positions(self) -> np.ndarray | None:
        return self._field("position")

    @property
    def normals(self) -> np.ndarray | None:
        return self._field("normal")

    @property
    def uvs(self) -> np.ndarray | None:
        return self._field("uv")

    @property
    def tangents(self) -> np.ndarray | None:
        return self._field("tangent")

    @property
    def binormals(self) -> np.ndarray | None:
        return self._field("binormal")

    @property
    def bone_indices(self) -> np.ndarray | None:
        """セクションのボーンパレット内インデックス (count, 4) uint8。"""
        return self._field("bone_indices")

    @property
    def weights(self) -> np.ndarray | None:
        return self._field("weights")

    @property
    def colors(self) -> np.ndarray | None:
        return self._field("color")

    @property
    def is_skinned(self) -> bool:
        return ATTR_BONE_INDICES in self.attributes


# ======================================================================
# 5: 三角形とセクション
# ======================================================================
@dataclass
class Section:
    """描画セクション（マテリアルごとの三角形範囲とボーンパレット）。

    * ``material``: チャンク 7 のマテリアル番号
    * ``start``, ``count``: 三角形配列内の開始位置と個数（セクションは連続して並ぶ）
    * ``reserved``: 全サンプルで 0
    * ``center``, ``extent``: このセクションの頂点の AABB 中心と半径（float32 で
      ``(min+max)*0.5``, ``(max-min)*0.5``。全サンプルでビット一致）
    * ``bones``: ボーンパレット。頂点の ``bone_indices`` はこの配列への添字
    """

    material: int
    start: int
    count: int
    center: np.ndarray
    extent: np.ndarray
    bones: list[int] = field(default_factory=list)
    reserved: int = 0

    def to_bytes(self) -> bytes:
        b = struct.pack("<IIII", self.material, self.start, self.count, self.reserved)
        b += np.ascontiguousarray(self.center, dtype="<f4").tobytes()
        b += np.ascontiguousarray(self.extent, dtype="<f4").tobytes()
        b += struct.pack(f"<I{len(self.bones)}I", len(self.bones), *self.bones)
        return b


@dataclass
class TriangleChunk(Chunk):
    """チャンク 5: ``[u32 三角形数][u32 セクション数][u16 インデックス×3n][セクション…]``。"""

    indices: np.ndarray                 # shape (count, 3) uint16
    sections: list[Section] = field(default_factory=list)
    id: int = CHUNK_TRIANGLES

    @classmethod
    def parse(cls, payload: bytes) -> "TriangleChunk":
        if len(payload) < 8:
            raise MshError("chunk 5: too short")
        ntri, nsec = struct.unpack_from("<II", payload, 0)
        pos = 8
        need = ntri * 6
        if len(payload) < pos + need:
            raise MshError("chunk 5: truncated index data")
        idx = np.frombuffer(payload, dtype="<u2", count=ntri * 3, offset=pos).reshape(ntri, 3).copy()
        pos += need
        sections = []
        for _ in range(nsec):
            if pos + 40 > len(payload):
                raise MshError("chunk 5: truncated section")
            mat, start, count, reserved = struct.unpack_from("<IIII", payload, pos)
            pos += 16
            box = np.frombuffer(payload, dtype="<f4", count=6, offset=pos).copy()
            pos += 24
            nb = _u32(payload, pos)
            pos += 4
            if pos + 4 * nb > len(payload):
                raise MshError("chunk 5: truncated bone palette")
            bones = list(struct.unpack_from(f"<{nb}I", payload, pos))
            pos += 4 * nb
            sections.append(Section(mat, start, count, box[:3], box[3:], bones, reserved))
        if pos != len(payload):
            raise MshError(f"chunk 5: {len(payload) - pos} trailing bytes")
        return cls(idx, sections)

    def to_bytes(self) -> bytes:
        idx = np.ascontiguousarray(self.indices, dtype="<u2")
        # bytes の += は毎回全体を写すのでセクション数の二乗の時間になる（空のセクションを並べた細工したファイルで
        # 止まる）。部品を集めて 1 回で連結する
        parts = [struct.pack("<II", len(idx), len(self.sections)), idx.tobytes()]
        parts.extend(s.to_bytes() for s in self.sections)
        return b"".join(parts)

    @property
    def count(self) -> int:
        return len(self.indices)


# ======================================================================
# 6: ボーン表
# ======================================================================
@dataclass
class Bone:
    """ボーン 1 本分（88 バイト）。

    * ``name``: 32 バイト、NUL 詰め
    * ``first_child``, ``child_count``: 子ボーンは ``[first_child, first_child+child_count)``
      の範囲に連続して並ぶ（親より後ろ）。子が無いときは ``child_count=0``
    * ``matrix``: 4×3 行列（行 0-2 = 回転の基底ベクトル、行 3 = 平行移動）。
      親ボーン相対のローカル変換と推定（行ベクトル規約 ``p' = p·R + t``）
    """

    name: str
    first_child: int
    child_count: int
    matrix: np.ndarray  # shape (4, 3) float32

    @classmethod
    def parse(cls, entry: bytes) -> "Bone":
        name = entry[:_BONE_NAME_LEN].split(b"\0", 1)[0].decode("latin-1")
        fc, cc = struct.unpack_from("<II", entry, _BONE_NAME_LEN)
        mat = np.frombuffer(entry, dtype="<f4", count=12, offset=_BONE_NAME_LEN + 8).reshape(4, 3).copy()
        return cls(name, fc, cc, mat)

    def to_bytes(self) -> bytes:
        nb = _latin1(self.name, "bone name")
        if len(nb) > _BONE_NAME_LEN:
            raise MshError(f"bone name too long (max {_BONE_NAME_LEN}): {self.name!r}")
        return nb.ljust(_BONE_NAME_LEN, b"\0") + struct.pack("<II", self.first_child, self.child_count) + \
            np.ascontiguousarray(self.matrix, dtype="<f4").tobytes()

    @property
    def rotation(self) -> np.ndarray:
        return self.matrix[:3]

    @property
    def translation(self) -> np.ndarray:
        return self.matrix[3]


@dataclass
class BoneChunk(Chunk):
    """チャンク 6: ``[u32 ボーン数][88 バイト×n]``。"""

    bones: list[Bone] = field(default_factory=list)
    id: int = CHUNK_BONES

    @classmethod
    def parse(cls, payload: bytes) -> "BoneChunk":
        n = _u32(payload, 0)
        if 4 + n * _BONE_ENTRY_LEN != len(payload):
            raise MshError(f"chunk 6: {n} bones but payload is {len(payload)} bytes")
        return cls([Bone.parse(payload[4 + i * _BONE_ENTRY_LEN: 4 + (i + 1) * _BONE_ENTRY_LEN]) for i in range(n)])

    def to_bytes(self) -> bytes:
        return struct.pack("<I", len(self.bones)) + b"".join(b.to_bytes() for b in self.bones)

    def __len__(self) -> int:
        return len(self.bones)

    def index(self, name: str) -> int:
        for i, b in enumerate(self.bones):
            if b.name == name:
                return i
        raise KeyError(name)

    def parents(self) -> list[int]:
        """各ボーンの親インデックス（根は -1）を first_child/child_count から復元する。

        子の範囲はボーン数で切り詰め（child_count が 0xFFFFFFFF でも反復はボーン数まで）、親より前（自分を含む）を
        指す子は無視する（親は常に子より前になり、前から順に姿勢を積み上げられる）。このような表は
        :meth:`MshFile.validate` が問題として報告する。
        """
        n = len(self.bones)
        parent = [-1] * n
        for i, b in enumerate(self.bones):
            for c in range(max(b.first_child, i + 1), min(b.first_child + b.child_count, n)):
                parent[c] = i
        return parent


# ======================================================================
# 8: ヒットボックス
# ======================================================================
@dataclass
class Hitbox:
    """ヒットボックス 1 個分（96 バイト）: 名前 32 バイト、float×15、i32。

    float×15 は半径 xyz、3×3 行列、平行移動 xyz と推定。末尾の i32 は基準ボーン番号
    （-1 = なし）と推定。
    """

    name: str
    extent: np.ndarray      # shape (3,)
    rotation: np.ndarray    # shape (3, 3)
    translation: np.ndarray  # shape (3,)
    bone: int = -1

    @classmethod
    def parse(cls, entry: bytes) -> "Hitbox":
        name = entry[:32].split(b"\0", 1)[0].decode("latin-1")
        f = np.frombuffer(entry, dtype="<f4", count=15, offset=32).copy()
        bone = struct.unpack_from("<i", entry, 92)[0]
        return cls(name, f[:3], f[3:12].reshape(3, 3), f[12:15], bone)

    def to_bytes(self) -> bytes:
        nb = _latin1(self.name, "hitbox name")
        if len(nb) > 32:
            raise MshError(f"hitbox name too long: {self.name!r}")
        return nb.ljust(32, b"\0") + \
            np.ascontiguousarray(self.extent, dtype="<f4").tobytes() + \
            np.ascontiguousarray(self.rotation, dtype="<f4").tobytes() + \
            np.ascontiguousarray(self.translation, dtype="<f4").tobytes() + \
            struct.pack("<i", self.bone)


@dataclass
class HitboxChunk(Chunk):
    """チャンク 8: ``[u32 個数][96 バイト×n]``。"""

    hitboxes: list[Hitbox] = field(default_factory=list)
    id: int = CHUNK_HITBOXES

    @classmethod
    def parse(cls, payload: bytes) -> "HitboxChunk":
        n = _u32(payload, 0)
        if 4 + n * _HITBOX_ENTRY_LEN != len(payload):
            raise MshError(f"chunk 8: {n} entries but payload is {len(payload)} bytes")
        return cls([Hitbox.parse(payload[4 + i * 96: 4 + (i + 1) * 96]) for i in range(n)])

    def to_bytes(self) -> bytes:
        return struct.pack("<I", len(self.hitboxes)) + b"".join(h.to_bytes() for h in self.hitboxes)


# ======================================================================
# 7: マテリアル
# ======================================================================
@dataclass
class MaterialParam:
    """``[u32 名前長][名前][u32 型][値]``。値の型は :data:`PARAM_STRING` などを参照。"""

    name: str
    type: int
    value: Union[str, float, tuple]

    @classmethod
    def parse(cls, payload: bytes, pos: int) -> tuple["MaterialParam", int]:
        n = _u32(payload, pos)
        pos += 4
        name = payload[pos:pos + n].decode("latin-1")
        pos += n
        t = _u32(payload, pos)
        pos += 4
        if t == PARAM_STRING:
            n = _u32(payload, pos)
            pos += 4
            value: Union[str, float, tuple] = payload[pos:pos + n].decode("latin-1")
            pos += n
        elif t == PARAM_FLOAT:
            value = struct.unpack_from("<f", payload, pos)[0]
            pos += 4
        elif t == PARAM_FLOAT2:
            value = struct.unpack_from("<2f", payload, pos)
            pos += 8
        elif t == PARAM_FLOAT3:
            value = struct.unpack_from("<3f", payload, pos)
            pos += 12
        elif t == PARAM_FLOAT4:
            value = struct.unpack_from("<4f", payload, pos)
            pos += 16
        else:
            raise MshError(f"chunk 7: unknown parameter type {t} for {name!r}")
        return cls(name, t, value), pos

    def to_bytes(self) -> bytes:
        nb = _latin1(self.name, "material parameter name")
        b = struct.pack("<I", len(nb)) + nb + struct.pack("<I", self.type)
        if self.type == PARAM_STRING:
            vb = _latin1(str(self.value), f"material parameter {self.name}")
            b += struct.pack("<I", len(vb)) + vb
        elif self.type == PARAM_FLOAT:
            b += struct.pack("<f", float(self.value))
        elif self.type == PARAM_FLOAT2:
            b += struct.pack("<2f", *self.value)
        elif self.type == PARAM_FLOAT3:
            b += struct.pack("<3f", *self.value)
        elif self.type == PARAM_FLOAT4:
            b += struct.pack("<4f", *self.value)
        else:
            raise MshError(f"unknown parameter type {self.type}")
        return b


@dataclass
class Material:
    """``[u32 シェーダーパス長][パス][u32 パラメータ数][パラメータ…]``。"""

    shader: str
    params: list[MaterialParam] = field(default_factory=list)

    def get(self, name: str, default=None):
        for p in self.params:
            if p.name == name:
                return p.value
        return default

    def set(self, name: str, type: int, value) -> None:
        for p in self.params:
            if p.name == name:
                p.type, p.value = type, value
                return
        self.params.append(MaterialParam(name, type, value))

    @classmethod
    def parse(cls, payload: bytes, pos: int) -> tuple["Material", int]:
        n = _u32(payload, pos)
        pos += 4
        shader = payload[pos:pos + n].decode("latin-1")
        pos += n
        nparams = _u32(payload, pos)
        pos += 4
        params = []
        for _ in range(nparams):
            p, pos = MaterialParam.parse(payload, pos)
            params.append(p)
        return cls(shader, params), pos

    def to_bytes(self) -> bytes:
        sb = _latin1(self.shader, "material shader")
        return struct.pack("<I", len(sb)) + sb + struct.pack("<I", len(self.params)) + \
            b"".join(p.to_bytes() for p in self.params)


@dataclass
class MaterialChunk(Chunk):
    """チャンク 7: ``[u32 マテリアル数][マテリアル…]``。"""

    materials: list[Material] = field(default_factory=list)
    id: int = CHUNK_MATERIALS

    @classmethod
    def parse(cls, payload: bytes) -> "MaterialChunk":
        n = _u32(payload, 0)
        pos = 4
        mats = []
        for _ in range(n):
            m, pos = Material.parse(payload, pos)
            mats.append(m)
        if pos != len(payload):
            raise MshError(f"chunk 7: {len(payload) - pos} trailing bytes")
        return cls(mats)

    def to_bytes(self) -> bytes:
        return struct.pack("<I", len(self.materials)) + b"".join(m.to_bytes() for m in self.materials)

    def __len__(self) -> int:
        return len(self.materials)


# ======================================================================
# 3: アタッチポイント等のテキスト
# ======================================================================
@dataclass
class AttachChunk(Chunk):
    """チャンク 3: 長さ接頭辞なしのテキスト（CRLF）。``AttachPoint { ... }`` や
    ``CreateEntity { ... }`` のブロックが並ぶ。"""

    text: str
    id: int = CHUNK_ATTACH

    @classmethod
    def parse(cls, payload: bytes) -> "AttachChunk":
        return cls(payload.decode("latin-1"))

    def to_bytes(self) -> bytes:
        return _latin1(self.text, "chunk 3 attach text")


# ======================================================================
# ファイル全体
# ======================================================================
_PARSERS: dict[int, type[Chunk]] = {
    CHUNK_SOURCE: SourceChunk,
    CHUNK_FLAGS: FlagsChunk,
    CHUNK_BBOX: BBoxChunk,
    CHUNK_VERTICES: VertexChunk,
    CHUNK_TRIANGLES: TriangleChunk,
    CHUNK_BONES: BoneChunk,
    CHUNK_HITBOXES: HitboxChunk,
    CHUNK_MATERIALS: MaterialChunk,
    CHUNK_ATTACH: AttachChunk,
}


def parse_chunk(chunk_id: int, payload: bytes, strict: bool = False) -> Chunk:
    """チャンク 1 個を型付きオブジェクトにする。

    ``strict=False`` のとき解釈に失敗したチャンクは :class:`RawChunk` で返す。
    ``strict=True`` のときは :class:`MshError` を送出する。
    """
    cls = _PARSERS.get(chunk_id)
    if cls is None:
        return RawChunk(chunk_id, bytes(payload))
    try:
        return cls.parse(payload)
    except (MshError, struct.error, UnicodeDecodeError, ValueError) as e:
        if strict:
            if isinstance(e, MshError):
                raise
            raise MshError(f"chunk {chunk_id}: {e}") from e
        return RawChunk(chunk_id, bytes(payload))


@dataclass
class MshFile:
    """``.msh`` 1 ファイル。``chunks`` はファイル内の順序を保つ。"""

    chunks: list[Chunk] = field(default_factory=list)
    version: int = VERSION

    # -- チャンク取得（同 ID が複数あれば最初のもの） --
    def find(self, chunk_id: int) -> Chunk | None:
        for c in self.chunks:
            if c.id == chunk_id:
                return c
        return None

    def _typed(self, chunk_id: int, cls):
        c = self.find(chunk_id)
        return c if isinstance(c, cls) else None

    @property
    def source(self) -> SourceChunk | None:
        return self._typed(CHUNK_SOURCE, SourceChunk)

    @property
    def flags(self) -> FlagsChunk | None:
        return self._typed(CHUNK_FLAGS, FlagsChunk)

    @property
    def bbox(self) -> BBoxChunk | None:
        return self._typed(CHUNK_BBOX, BBoxChunk)

    @property
    def vertices(self) -> VertexChunk | None:
        return self._typed(CHUNK_VERTICES, VertexChunk)

    @property
    def triangles(self) -> TriangleChunk | None:
        return self._typed(CHUNK_TRIANGLES, TriangleChunk)

    @property
    def bones(self) -> BoneChunk | None:
        return self._typed(CHUNK_BONES, BoneChunk)

    @property
    def hitboxes(self) -> HitboxChunk | None:
        return self._typed(CHUNK_HITBOXES, HitboxChunk)

    @property
    def materials(self) -> MaterialChunk | None:
        return self._typed(CHUNK_MATERIALS, MaterialChunk)

    @property
    def attach(self) -> AttachChunk | None:
        return self._typed(CHUNK_ATTACH, AttachChunk)

    @property
    def raw_chunks(self) -> list[RawChunk]:
        return [c for c in self.chunks if isinstance(c, RawChunk)]

    def replace(self, chunk: Chunk) -> None:
        """同じ ID のチャンクを置き換える（無ければ末尾に追加）。"""
        for i, c in enumerate(self.chunks):
            if c.id == chunk.id:
                self.chunks[i] = chunk
                return
        self.chunks.append(chunk)

    # -- 整合性検査 --
    def validate(self) -> list[str]:
        """内部整合性を調べ、問題点の一覧（空なら問題なし）を返す。

        解釈できなかった（:class:`RawChunk` のまま残った）チャンクや未知の頂点レイアウトは、その旨を
        問題として 1 行で報告し、中身を使う検査は飛ばす（例外や誤った問題を出さないため）。
        チャンクの構成（:data:`REQUIRED_CHUNKS` が 1 個ずつこの順に）・ボーンの階層・ヒットボックスの基準ボーンの
        番号・inf の頂点位置も調べる。
        """
        problems: list[str] = []
        v, t, b, m = self.vertices, self.triangles, self.bones, self.materials
        raw_ids = {c.id for c in self.raw_chunks}
        # チャンクの構成（必須チャンクの欠け・重複・順序と、任意のチャンクの重複）。同じ ID が 2 個あると、ここでも
        # modbuilder の検査でも最初の 1 個しか見ないので、2 個目の頂点・三角形・マテリアルが検査されずに通ってしまう
        ids = [c.id for c in self.chunks]
        for cid in REQUIRED_CHUNKS:
            k = ids.count(cid)
            if k > 1:
                problems.append(f"chunk {cid} appears {k} times (expected 1)")
            elif k == 0 and cid not in (CHUNK_VERTICES, CHUNK_TRIANGLES):   # 頂点・三角形の欠けは下で報告する
                problems.append(f"no chunk {cid}")
        for cid in _OPTIONAL_SINGLE_CHUNKS:
            k = ids.count(cid)
            if k > 1:
                problems.append(f"chunk {cid} appears {k} times (expected at most 1)")
        core = [i for i in ids if i in REQUIRED_CHUNKS]
        if sorted(core) == sorted(REQUIRED_CHUNKS) and tuple(core) != REQUIRED_CHUNKS:
            problems.append(f"required chunks out of order: {core} (expected {list(REQUIRED_CHUNKS)})")
        # ボーン表の階層（子は [first_child, first_child+child_count) に連続し、親より後ろでボーン数の内側）と、
        # ヒットボックスの基準ボーン（-1 か 0..ボーン数-1）。ボーン表が解釈できなかったときは番号の検査を飛ばす
        bones_known = not (b is None and CHUNK_BONES in raw_ids)
        nbones = len(b.bones) if b else 0
        if b is not None:
            for i, bone in enumerate(b.bones):
                fc, cc = bone.first_child, bone.child_count
                if cc and not (i < fc and fc + cc <= nbones):
                    problems.append(f"bone {i}: children {fc}..{fc + cc - 1} are not after it within bone count {nbones}")
        hb = self.hitboxes
        if hb is not None and bones_known:
            for j, h in enumerate(hb.hitboxes):
                if not -1 <= h.bone < nbones:
                    problems.append(f"hitbox {j}: bone {h.bone} not in -1..{nbones - 1}")
        if v is None:
            problems.append("vertex chunk not interpreted" if CHUNK_VERTICES in raw_ids else "no vertex chunk")
        if t is None:
            problems.append("triangle chunk not interpreted" if CHUNK_TRIANGLES in raw_ids else "no triangle chunk")
        if v is None or t is None:
            return problems
        nv = v.count
        idx_ok = not (t.count and t.indices.max() >= nv)     # 範囲外の番号があれば頂点の中身の検査は飛ばす
        if not idx_ok:
            problems.append(f"triangle index {t.indices.max()} >= vertex count {nv}")
        if nv > 65535:
            problems.append(f"vertex count {nv} exceeds uint16 index range")
        if v.data is None:
            problems.append(f"unknown vertex layout (attributes {list(v.attributes)}, stride {v.stride}); "
                            "vertex checks skipped")
        # inf の位置は外接箱・中心も inf になって比較が一致してしまうので、別に報告する（NaN の位置は外接箱・中心の
        # 不一致として報告される。公式ファイルにも NaN の位置の頂点がある）
        if nv and v.positions is not None:
            inf = np.isinf(v.positions).any(axis=1)
            if inf.any():
                problems.append(f"vertex positions contain inf ({int(inf.sum())} vertices, first {int(np.argmax(inf))})")
        # マテリアル・ボーン表が解釈できなかったときは、番号の検査を飛ばす（数 0 との比較で誤検知しない）
        mat_known = not (m is None and CHUNK_MATERIALS in raw_ids)
        if not mat_known:
            problems.append("material chunk not interpreted; material index checks skipped")
        if not bones_known:
            problems.append("bone chunk not interpreted; palette bone checks skipped")
        expect = 0
        nmat = len(m.materials) if m else 0
        bi_all, w_all = v.bone_indices, v.weights      # 未知のレイアウトでは None
        for i, s in enumerate(t.sections):
            if s.start != expect:
                problems.append(f"section {i}: start {s.start} != expected {expect}")
            expect = s.start + s.count
            if mat_known and s.material >= nmat:
                problems.append(f"section {i}: material {s.material} >= material count {nmat}")
            if bones_known:
                for bi in s.bones:
                    if bi >= nbones:
                        problems.append(f"section {i}: palette bone {bi} >= bone count {nbones}")
            if v.is_skinned and len(s.bones) > MAX_PALETTE:
                problems.append(f"section {i}: palette has {len(s.bones)} bones > shader limit {MAX_PALETTE}")
            # 範囲が三角形の配列の外にあるセクション（ループの後の「sections cover」で報告）では used が空になりうる
            used = np.unique(t.indices[s.start:s.start + s.count]) if (s.count and idx_ok) else None
            if used is None or not len(used):
                continue
            if v.is_skinned and bi_all is not None and w_all is not None:
                bi = bi_all[used]
                w = w_all[used]
                active = w > 0
                if active.any() and bi[active].max() >= len(s.bones):
                    problems.append(f"section {i}: vertex palette index {bi[active].max()} >= palette size {len(s.bones)}")
            if v.positions is not None:
                p = v.positions[used]
                mn, mx = p.min(axis=0), p.max(axis=0)
                c = (mn + mx) * np.float32(0.5)
                e = (mx - mn) * np.float32(0.5)
                if not (np.allclose(c, s.center, atol=1e-4) and np.allclose(e, s.extent, atol=1e-4)):
                    problems.append(f"section {i}: center/extent do not match vertices")
        if expect != t.count:
            problems.append(f"sections cover {expect} triangles, chunk has {t.count}")
        bb = self.bbox
        if bb is not None and nv and v.positions is not None:
            if not (np.allclose(v.positions.min(axis=0), bb.min, atol=1e-5) and
                    np.allclose(v.positions.max(axis=0), bb.max, atol=1e-5)):
                problems.append("bounding box does not match vertex positions")
        return problems


# ======================================================================
# 入出力
# ======================================================================
def iter_raw_chunks(data: bytes) -> Iterator[tuple[int, bytes]]:
    """ヘッダー検証後に ``(chunk_id, payload)`` を順に返す。"""
    if len(data) < 4 or data[:3] != MAGIC:
        raise MshError("not a MSH file (bad magic)")
    pos = 4
    while pos < len(data):
        if pos + 8 > len(data):
            raise MshError(f"truncated chunk header at {pos}")
        cid, size = struct.unpack_from("<II", data, pos)
        pos += 8
        if pos + size > len(data):
            raise MshError(f"chunk {cid} at {pos - 8}: size {size} exceeds file")
        yield cid, data[pos:pos + size]
        pos += size


def read_bytes(data: bytes, strict: bool = False) -> MshFile:
    """バイト列から :class:`MshFile` を作る。

    厳密モードでは version が :data:`VERSION`（3。ゲームの全 .msh がこの値）以外のファイルも拒否する。
    厳密でないモードでは version をそのまま保持する（往復はバイト一致）。
    """
    # 4 バイト未満（空・途中で切れたファイル）でも IndexError でなく MshError にするため、先にヘッダーを確かめる
    if len(data) < 4 or data[:3] != MAGIC:
        raise MshError(f"not a MSH file ({len(data)} bytes; too short or bad magic)")
    version = data[3]
    if strict and version != VERSION:
        raise MshError(f"unsupported MSH version {version} (expected {VERSION})")
    chunks = [parse_chunk(cid, payload, strict=strict) for cid, payload in iter_raw_chunks(data)]
    return MshFile(chunks, version)


def read(path_or_file: Union[str, "os.PathLike[str]", BinaryIO], strict: bool = False) -> MshFile:
    """ファイルパスまたはバイナリストリームから読み込む。"""
    if hasattr(path_or_file, "read"):
        data = path_or_file.read()
    else:
        with open(path_or_file, "rb") as f:
            data = f.read()
    return read_bytes(data, strict=strict)


def to_bytes(msh: MshFile) -> bytes:
    """:class:`MshFile` をファイルのバイト列にする。"""
    out = bytearray(MAGIC + bytes([msh.version]))
    for c in msh.chunks:
        payload = c.to_bytes()
        out += struct.pack("<II", c.id, len(payload))
        out += payload
    return bytes(out)


def write(msh: MshFile, path_or_file: Union[str, "os.PathLike[str]", BinaryIO]) -> None:
    """ファイルパスまたはバイナリストリームに書き出す。"""
    data = to_bytes(msh)
    if hasattr(path_or_file, "write"):
        path_or_file.write(data)
    else:
        with open(path_or_file, "wb") as f:
            f.write(data)


# ---------------------------------------------------------------------
def _u32(b: bytes, pos: int) -> int:
    if pos + 4 > len(b):
        raise MshError(f"unexpected end of data at {pos}")
    return struct.unpack_from("<I", b, pos)[0]


import os  # noqa: E402  (型注釈用)
