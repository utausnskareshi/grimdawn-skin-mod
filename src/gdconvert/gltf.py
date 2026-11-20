"""glTF 2.0 / GLB の読み込み（変換に必要な範囲だけ。依存は numpy と Pillow）。

読み込むもの:

* シーンのノード階層（matrix / TRS）を適用したワールド座標の三角形メッシュ。スキン付きのメッシュは、仕様どおり
  メッシュのノードの変換を使わず、ジョイントのワールド変換 × inverseBindMatrices のウェイト付きの和で静止形にする
* 属性 POSITION, TEXCOORD_n（ベースカラーのテクスチャの texCoord で選ぶ）, COLOR_0, JOINTS_n / WEIGHTS_n（静止形の計算だけ）
* マテリアルの baseColorTexture（PNG/JPEG/WebP。bufferView / data URI / GLB のフォルダーからの相対参照の外部ファイル）と baseColorFactor、
  KHR_texture_transform（UV に適用）、KHR_materials_pbrSpecularGlossiness の diffuse、alphaMode / alphaCutoff

扱わないもの: スパースアクセサー、必須の圧縮拡張（Draco・meshopt）と、:data:`SUPPORTED_EXT` に無い必須の拡張
（:class:`GltfError`。``reason`` に理由のコード）。モーフターゲット・三角形以外のプリミティブ・サンプラーの
繰り返し方法は無視して警告にする。利用者に見せる警告は :attr:`Model.notices` に短いキー（``load_*``）でも持つ。
"""
from __future__ import annotations

import base64
import io
import json
import numbers
import os
import struct
from dataclasses import dataclass, field

import numpy as np

from ._i18n import tr

__all__ = ["GltfError", "Material", "Model", "SUPPORTED_EXT", "load", "load_glb_bytes", "resolve_external_uri",
           "save_glb", "srgb_to_linear", "linear_to_srgb", "uv_transform_matrix"]

_COMPONENT = {
    5120: np.int8, 5121: np.uint8, 5122: np.int16,
    5123: np.uint16, 5125: np.uint32, 5126: np.float32,
}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_NORM_DIV = {np.int8: 127.0, np.uint8: 255.0, np.int16: 32767.0, np.uint16: 65535.0, np.uint32: 4294967295.0}
# 圧縮の拡張: 必須（extensionsRequired）なら読めない。extensionsUsed だけなら非圧縮の代替データを読む
_COMPRESSION_EXT = {"KHR_draco_mesh_compression", "EXT_meshopt_compression", "KHR_meshopt_compression"}
# 必須でも読める拡張（形とベースカラーを正しく読めるもの、または無視しても形とベースカラーが変わらないもの）。
# これ以外が extensionsRequired にあれば GltfError（reason='unsupported_extension'）
SUPPORTED_EXT = {
    "KHR_texture_transform", "KHR_mesh_quantization", "KHR_materials_pbrSpecularGlossiness", "EXT_texture_webp",
    "KHR_materials_unlit", "KHR_materials_emissive_strength", "KHR_materials_ior", "KHR_materials_specular",
    "KHR_materials_clearcoat", "KHR_materials_sheen", "KHR_materials_transmission", "KHR_materials_volume",
    "KHR_materials_iridescence", "KHR_materials_anisotropy", "KHR_materials_dispersion", "KHR_materials_variants",
    "KHR_lights_punctual", "KHR_xmp_json_ld", "KHR_animation_pointer",
}
# テクスチャの画像を拡張の source から読める拡張（KHR_texture_basisu の KTX2 は Pillow で読めない）
_IMAGE_EXT = ("EXT_texture_webp",)
# 埋め込み画像として開く形式（glTF の PNG・JPEG と EXT_texture_webp の WebP、ほかの道具が書く BMP）。EPS など外部の
# プログラムを起動する形式は開かない（点検 R10B-04）
_IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "BMP")
_REPEAT = 10497
# 読み込みの大きさの上限（数百バイトの細工した・壊れた GLB が数 GB〜数十 GB を確保しないように。超えたら GltfError の
# reason 'too_large'。2026-10-27 点検 R02A-01・R02B-01）
_MAX_LOAD_VERTICES = 20_000_000     # シーンに置く頂点の合計（インスタンスごとに数える）。インデックスはこの 6 倍まで
_MAX_ZERO_COUNT = 1 << 20           # bufferView の無いアクセサー（スパースは未対応なので中身は 0 だけ）の要素数
_MAX_NODE_VISITS = 100_000          # ノードの訪問回数の下限の上限（実際の上限は max(4 × ノード数, これ)）
_MAX_PRIM_INSTANCES = 100_000       # シーンに置くプリミティブの数（インスタンスごとに数える。頂点 0 個のものも数える）
# 同じデータの使い回し（インスタンス・同じ bufferView を指す多数のアクセサー・データの無いアクセサー）で増やせる
# 頂点の数。置く頂点の合計は「これ + POSITION の bufferView のバイト数 ÷ 3」まで（明示のインデックスは
# 「6 × これ + インデックスの bufferView のバイト数」まで）。使い回しの無い大きなモデルは _MAX_LOAD_VERTICES まで読める
# （2026-10-29 G02 の見直し）
_MAX_REUSED_VERTICES = 2_000_000
# 1 つのファイルで復号する画像（同じデータの出どころは 1 回だけ数える）の画素数の合計。4096² の 16 枚分（RGBA で約 1 GiB）。
# TRELLIS の出力はテクスチャ 1 枚（最大 2048² か 4096²）なので届かない。小さな PNG（単色の 4096² は約 56 KB）を
# 指す images を並べて数 GB〜数十 GB を確保しないように（点検 R02B-02 の見直し）。超えたら GltfError の reason
# 'images_too_large'（頂点・ノードの 'too_large' と分ける。点検 D1_lib-03）
_MAX_IMAGE_PIXELS = 16 * 4096 * 4096


class GltfError(ValueError):
    """読めない（または変換に使えない）glTF。``reason`` は画面の文を選ぶ理由のコード（unsupported_extension・
    compressed・truncated・no_triangles・too_large（頂点・ノードの上限）・images_too_large（画像の画素数の合計の上限）
    など。無ければ None）、``params`` は文に入れる値。"""

    def __init__(self, message: str, reason: str | None = None, **params):
        super().__init__(message)
        self.reason = reason
        self.params = params


def resolve_external_uri(base_dir: str, uri: str) -> str:
    """外部参照の uri（data: 以外）→ GLB のフォルダーからのパス。

    glTF の外部参照は GLB のフォルダーからの相対参照なので、絶対パス・ドライブ名・UNC（//host/share）・
    デバイスパス（//?/）・スキーム（file: など）は、ファイルに触る前に字句だけで GltfError（reason
    ``external_uri``）にする（UNC を開くと Windows がそのホストに SMB で接続し、認証情報を送りうるため）。
    ``../`` を含む相対参照は正しい参照として読む。取り込み（app/backend の pipeline）も同じ規則を使う。"""
    from urllib.parse import unquote
    rel = unquote(uri)
    if not rel or rel[0] in "/\\" or ":" in rel:
        raise GltfError(tr(f"外部参照が GLB のフォルダーからの相対パスではない: {uri!r}",
                           f"The external reference is not a path relative to the GLB's folder: {uri!r}"),
                        "external_uri")
    return os.path.join(base_dir, rel)


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    """sRGB の伝達関数の逆（0〜1 → 0〜1。glTF 仕様の区分関数）。"""
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c: np.ndarray) -> np.ndarray:
    """線形 → sRGB（0〜1 → 0〜1）。"""
    c = np.clip(np.asarray(c, dtype=np.float64), 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def uv_transform_matrix(ext: dict) -> np.ndarray:
    """KHR_texture_transform（offset・rotation・scale）→ UV に掛ける 3×3 行列（uv' = M @ (u, v, 1)）。

    M = T・R・S。R は README の例（offset [0, 1]・rotation 90°・scale 0.5 で「元の画像の左下の 4 分の 1 を時計回りに
    90° 回して使う」）に合うように、行で書くと [[cos, sin], [-sin, cos]]（UV は v が下向きなので、見た目では
    UV を反時計回りに回す）。README の GLSL の mat3 は列優先なので、そのまま行列として読むと符号が逆になる点に注意。
    """
    tx, ty = (float(v) for v in ext.get("offset", (0.0, 0.0)))
    r = float(ext.get("rotation", 0.0))
    sx, sy = (float(v) for v in ext.get("scale", (1.0, 1.0)))
    c, s = np.cos(r), np.sin(r)
    T = np.array([[1, 0, tx], [0, 1, ty], [0, 0, 1]], dtype=np.float64)
    R = np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]], dtype=np.float64)
    S = np.diag([sx, sy, 1.0])
    return T @ R @ S


@dataclass
class Material:
    """変換に使う範囲のマテリアル。

    色空間: ``image`` は (h, w, 4) uint8 の sRGB の 8 bit 値（RGBA、行 0 = 画像の上端 = v 0。アルファは線形）、
    ``base_color_factor`` と :attr:`Model.colors`（COLOR_0）は線形の値（glTF 仕様）。合成は
    :func:`gdconvert.texture.single_texture` / :func:`~gdconvert.texture.bake_from_model` が線形で行う。
    ``texcoord`` はテクスチャが使う TEXCOORD_n の n、``uv_transform`` は KHR_texture_transform の行列（読み込みで
    UV に適用済み。記録用）、``alpha_mode`` は OPAQUE / MASK / BLEND（MASK は ``alpha_cutoff`` 未満を切り抜き）。
    ``image`` は同じ画像を使うマテリアルどうしで同じ配列を共有するので、読み取り専用として扱う。
    """

    name: str = ""
    base_color_factor: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    image: np.ndarray | None = None
    image_mime: str = ""
    texcoord: int = 0
    uv_transform: np.ndarray | None = None
    alpha_mode: str = "OPAQUE"
    alpha_cutoff: float = 0.5
    wrap: tuple[int, int] = (_REPEAT, _REPEAT)

    @property
    def has_texture(self) -> bool:
        return self.image is not None


@dataclass
class Model:
    """1 つにまとめた三角形メッシュ。

    positions (n,3) float64, uvs (n,2) float64 または None, colors (n,4) float32 または None（線形）,
    faces (m,3) int64（glTF どおり反時計回り）, face_material (m,) int64, materials list。
    ``warnings`` は読み込みの注意の全文（報告 JSON 用）。そのうち利用者に見せるものは ``notices`` に
    (短いキー, 文) で持つ（キーは ``load_<種類>`` か ``load_<種類>:<詳細>``。画面の文は Quality_load_<種類>）。
    """

    positions: np.ndarray
    faces: np.ndarray
    uvs: np.ndarray | None = None
    colors: np.ndarray | None = None
    face_material: np.ndarray | None = None
    materials: list[Material] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = ""
    notices: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self):
        if self.face_material is None:
            self.face_material = np.zeros(len(self.faces), dtype=np.int64)

    @property
    def vertex_count(self) -> int:
        return len(self.positions)

    @property
    def face_count(self) -> int:
        return len(self.faces)

    @property
    def load_flags(self) -> list[str]:
        """利用者に見せる読み込みの注意のキー（重複なし、出た順）。"""
        return list(dict.fromkeys(k for k, _ in self.notices))

    def copy(self) -> "Model":
        return Model(self.positions.copy(), self.faces.copy(),
                     None if self.uvs is None else self.uvs.copy(),
                     None if self.colors is None else self.colors.copy(),
                     self.face_material.copy(), list(self.materials), list(self.warnings), self.source,
                     list(self.notices))


# ----------------------------------------------------------------------
# 読み込み
# ----------------------------------------------------------------------
def load(path: str) -> Model:
    """``.glb`` または ``.gltf`` を読む。"""
    with open(path, "rb") as f:
        data = f.read()
    base_dir = os.path.dirname(os.path.abspath(path))
    if data[:4] == b"glTF":
        m = load_glb_bytes(data, base_dir)
    else:
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise GltfError(tr(f"{path}: GLB でも glTF JSON でもない: {e}",
                               f"{path}: neither a GLB nor glTF JSON: {e}")) from None
        m = _read_model(doc, None, base_dir)
    m.source = os.path.abspath(path)
    return m


def _read_model(doc, binchunk: bytes | None, base_dir: str) -> Model:
    """:class:`_Reader` で読む。glTF の構造の誤り（欠けたキー・型の違う値など）で出た Python の素の例外も、理由
    truncated の GltfError にする（英語の内部の文だけで理由の無いエラーにしない。点検 R02A-04・R02B-04）。
    元の例外は __cause__ に残す（gdconvert 自身の不具合も追えるように）。MemoryError などは包まない。"""
    try:
        return _Reader(doc, binchunk, base_dir).model()
    except GltfError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
        raise GltfError(tr(f"glTF の構造が正しくない（{type(e).__name__}: {e}）",
                           f"The glTF structure is invalid ({type(e).__name__}: {e})"), "truncated") from e


def load_glb_bytes(data: bytes, base_dir: str = ".") -> Model:
    if len(data) < 20 or data[:4] != b"glTF":
        raise GltfError(tr("GLB として短すぎるか、マジックナンバーがない", "Too short to be a GLB, or the GLB magic number is missing"))
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2:
        raise GltfError(tr(f"glTF バージョン {version} は未対応（2 のみ）", f"glTF version {version} is not supported (only 2)"))
    pos = 12
    doc = has_json = None       # JSON の null（最上位が null）と「JSON チャンクが無い」を区別する
    binchunk = None
    # ヘッダーの全長かチャンクの長さが実際のファイルより長い = 途中で切れている。そのときの理由の無い失敗（画像を
    # 読めない・BIN チャンクが無いなど）は truncated にする（点検 R02A-05。今読めているファイルは拒まない）
    short = length > len(data)
    while pos + 8 <= min(length, len(data)):
        clen, ctype = struct.unpack_from("<I4s", data, pos)
        if pos + 8 + clen > len(data):
            short = True
        body = data[pos + 8:pos + 8 + clen]
        if ctype == b"JSON":
            try:
                doc = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise GltfError(tr(f"GLB の JSON チャンクを読めない（ファイルが壊れている可能性）: {e}",
                                   f"Cannot read the GLB's JSON chunk (the file may be corrupted): {e}"),
                                "truncated") from None
            has_json = True
        elif ctype == b"BIN\x00" and binchunk is None:
            binchunk = body
        pos += 8 + clen
    if not has_json:
        raise GltfError(tr("GLB に JSON チャンクがない", "The GLB has no JSON chunk"), "truncated" if short else None)
    try:
        return _read_model(doc, binchunk, base_dir)
    except GltfError as e:
        if e.reason is None and short:
            raise GltfError(tr(f"GLB が途中で切れている（ヘッダーの長さ {length} バイト、ファイル {len(data)} バイト）: {e}",
                               f"The GLB is cut off (header length {length} bytes, file {len(data)} bytes): {e}"),
                            "truncated") from None
        raise


class _Reader:
    def __init__(self, doc: dict, binchunk: bytes | None, base_dir: str):
        # 最上位が配列・null の JSON は、doc.get の AttributeError にしない（点検 R02A-04）
        if not isinstance(doc, dict):
            raise GltfError(tr("glTF の JSON の最上位がオブジェクトではない", "The top level of the glTF JSON is not an object"),
                            "truncated")
        self.doc = doc
        self.bin = binchunk
        self.base_dir = base_dir
        self.warnings: list[str] = []
        self.notices: list[tuple[str, str]] = []
        self._buffers: dict[int, bytes] = {}
        self._uri_data: dict[str, bytes] = {}
        self._images: dict[int, tuple[np.ndarray, str]] = {}
        # 復号した画像（配列, 形式から決めた mime）。鍵はデータの出どころ（("bufferView", 番号) か ("uri", 文字列)）
        self._image_data: dict[tuple, tuple[np.ndarray, str]] = {}
        self._image_pixels = 0          # 復号した画像の画素数の合計（_MAX_IMAGE_PIXELS と比べる）
        self._warned: set[str] = set()
        req = doc.get("extensionsRequired", [])
        if not isinstance(req, list) or not all(isinstance(x, str) for x in req):
            # 文字列のままだと 1 文字ずつの集合になり「未対応の必須の拡張: H, K, R, …」になっていた
            raise GltfError(tr(f"extensionsRequired が文字列の配列ではない: {req!r}",
                               f"extensionsRequired is not an array of strings: {req!r}"), "truncated")
        required = set(req)
        comp = required & _COMPRESSION_EXT
        if comp:
            names = ", ".join(sorted(comp))
            raise GltfError(tr(f"圧縮された GLB（{names}）は読めない（圧縮なしで書き出し直す）",
                               f"A compressed GLB ({names}) cannot be read (export it again without compression)"),
                            "compressed", extensions=names)
        bad = required - SUPPORTED_EXT
        if bad:
            names = ", ".join(sorted(bad))
            raise GltfError(tr(f"未対応の必須の拡張: {names}", f"Unsupported required extensions: {names}"),
                            "unsupported_extension", extensions=names)

    def warn(self, message: str, key: str | None = None) -> None:
        """読み込みの注意。``key`` があれば利用者に見せる（同じ文は 1 回だけ）。"""
        if message in self._warned:
            return
        self._warned.add(message)
        self.warnings.append(message)
        if key:
            self.notices.append((key, message))

    def _item(self, kind: str, i) -> dict:
        """``doc[kind][i]``。番号は bool でない 0 以上の int で範囲内、要素はオブジェクトであることを確かめる（負の番号が
        Python の添字で末尾の要素を黙って指したり、欠けた配列が KeyError になったりしないように。点検 R02B-04）。"""
        arr = self.doc.get(kind)
        if (not isinstance(arr, list) or isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(arr)
                or not isinstance(arr[i], dict)):
            raise GltfError(tr(f"{kind} の番号 {i!r} が範囲外か不正", f"{kind} index {i!r} is out of range or invalid"),
                            "truncated")
        return arr[i]

    def _count(self, i) -> int:
        """アクセサー i の要素数（JSON の値だけ。データは読まない）。bool でない 0 以上の int でなければ GltfError。"""
        count = self._item("accessors", i).get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise GltfError(tr(f"accessor {i}: count が不正: {count!r}", f"accessor {i}: invalid count: {count!r}"),
                            "truncated")
        return count

    def _offset(self, d: dict, key: str, what: str) -> int:
        """byteOffset・byteLength などの 0 以上の int（無ければ 0）。"""
        v = d.get(key, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise GltfError(tr(f"{what} の {key} が不正: {v!r}", f"{what}: invalid {key}: {v!r}"), "truncated")
        return v

    def _check_type(self, i, what: str, types: tuple, components: tuple | None = None) -> None:
        """属性・インデックスのアクセサーの type（と componentType）が仕様どおりか確かめる（POSITION が VEC2 で行列の
        掛け算の英語の文になる、COLOR_0 が SCALAR で後段の IndexError になるなどを、読み込みで止める。点検 R02A-04）。"""
        a = self._item("accessors", i)
        if a.get("type") not in types or (components is not None and a.get("componentType") not in components):
            raise GltfError(tr(f"{what} のアクセサー {i} の型が不正（type {a.get('type')}, componentType "
                               f"{a.get('componentType')}）",
                               f"The {what} accessor {i} has an invalid type (type {a.get('type')}, componentType "
                               f"{a.get('componentType')})"), "truncated")

    # -- バッファ --
    def buffer(self, i: int) -> bytes:
        b = self._item("buffers", i)
        if i in self._buffers:
            return self._buffers[i]
        uri = b.get("uri")
        if uri is not None and not isinstance(uri, str):
            raise GltfError(tr(f"buffer {i} の uri が文字列ではない: {uri!r}", f"buffer {i}: the uri is not a string: {uri!r}"),
                            "truncated")
        if uri is None:
            # uri の無いバッファは GLB の BIN チャンク（仕様では番号 0 だけ。meshopt の fallback などのデータの無い
            # バッファを BIN と取り違えない）
            if i != 0:
                raise GltfError(tr(f"buffer {i} に uri が無い（データの無いバッファ。圧縮の拡張の代替用の可能性）",
                                   f"buffer {i} has no uri (a buffer without data, possibly the fallback of a "
                                   "compression extension)"),
                                "compressed", extensions="EXT_meshopt_compression")
            if self.bin is None:
                raise GltfError(tr(f"buffer {i} に uri も BIN チャンクもない", f"buffer {i} has neither a uri nor a BIN chunk"))
            data = self.bin
        else:
            # 同じ uri の複数のバッファは 1 回だけ読んで同じバイト列を使う（同じファイルを指すバッファを並べて、読み込みの
            # 数だけ確保させない。2026-10-29 G02 の見直し）
            data = self._uri_data.get(uri)
            if data is None:
                data = self._uri_data[uri] = self._read_uri(uri)
        self._buffers[i] = data
        return data

    def _read_uri(self, uri: str) -> bytes:
        if uri.startswith("data:"):
            try:
                return base64.b64decode(uri.split(",", 1)[1])
            except Exception as e:  # noqa: BLE001
                raise GltfError(tr(f"data URI を復号できない: {e}", f"Cannot decode the data URI: {e}")) from None
        p = resolve_external_uri(self.base_dir, uri)
        if not os.path.isfile(p):
            raise GltfError(tr(f"外部ファイルがない: {p}", f"External file not found: {p}"))
        with open(p, "rb") as f:
            return f.read()

    def buffer_view(self, i: int) -> tuple[bytes, int, int | None]:
        bv = self._item("bufferViews", i)
        buf = self.buffer(bv.get("buffer"))
        off = self._offset(bv, "byteOffset", f"bufferView {i}")
        stride = bv.get("byteStride")
        if stride is not None and (isinstance(stride, bool) or not isinstance(stride, int) or stride < 0):
            raise GltfError(tr(f"bufferView {i} の byteStride が不正: {stride!r}",
                               f"bufferView {i}: invalid byteStride: {stride!r}"), "truncated")
        return buf, off, stride

    def _view_bytes(self, accessors) -> int:
        """アクセサーの集まりが指す bufferView のデータのバイト数（同じデータ（同じ uri のバッファ）の中で重なる範囲は
        1 回だけ数える。bufferView の無いアクセサーは 0）。同じデータの使い回しの上限に使う（2026-10-29 G02 の見直し）。"""
        spans: dict[str | None, list[tuple[int, int]]] = {}
        for v in {self._item("accessors", i).get("bufferView") for i in accessors} - {None}:
            buf, off, _ = self.buffer_view(v)
            bv = self._item("bufferViews", v)
            end = len(buf)
            if "byteLength" in bv:
                end = min(end, off + self._offset(bv, "byteLength", f"bufferView {v}"))
            if end > off:
                spans.setdefault(self._item("buffers", bv.get("buffer")).get("uri"), []).append((off, end))
        total = 0
        for lst in spans.values():
            lst.sort()
            s0, e0 = lst[0]
            for s, e in lst[1:]:
                if s > e0:
                    total += e0 - s0
                    s0, e0 = s, e
                else:
                    e0 = max(e0, e)
            total += e0 - s0
        return total

    def accessor(self, i: int) -> np.ndarray:
        a = self._item("accessors", i)
        if "sparse" in a:
            raise GltfError(tr(f"accessor {i}: スパースアクセサーは未対応", f"accessor {i}: sparse accessors are not supported"))
        if a.get("componentType") not in _COMPONENT or a.get("type") not in _NCOMP:
            raise GltfError(tr(f"accessor {i}: 型が不正（componentType {a.get('componentType')}, type {a.get('type')}）",
                               f"accessor {i}: invalid type (componentType {a.get('componentType')}, "
                               f"type {a.get('type')})"))
        dtype = np.dtype(_COMPONENT[a["componentType"]]).newbyteorder("<")
        ncomp = _NCOMP[a["type"]]
        count = self._count(i)
        if "bufferView" not in a or count == 0:
            # スパースは未対応なので中身は 0 だけ。ファイルの大きさと無関係に JSON の数だけで決まるので、上限を置く
            # （672 バイトの GLB で数 GB を確保していた。点検 R02A-01）
            if count > _MAX_ZERO_COUNT:
                raise GltfError(tr(f"accessor {i}: データ（bufferView）の無いアクセサーの要素数 {count} が多すぎる"
                                   f"（上限 {_MAX_ZERO_COUNT}）",
                                   f"accessor {i}: the accessor without data (bufferView) has too many elements "
                                   f"({count}; the limit is {_MAX_ZERO_COUNT})"),
                                "too_large", vertices=count, limit=_MAX_ZERO_COUNT)
            out = np.zeros((count, ncomp), dtype=dtype)
        else:
            buf, off, stride = self.buffer_view(a["bufferView"])
            bv = self._item("bufferViews", a["bufferView"])
            aoff = self._offset(a, "byteOffset", f"accessor {i}")
            off += aoff
            elem = dtype.itemsize * ncomp
            if stride and stride < elem:
                # 要素が重なったまま読む（負のストライドはバッファの外を読む）ことにしない（点検 R02A-03）
                raise GltfError(tr(f"accessor {i}: bufferView の byteStride {stride} が要素の大きさ {elem} より小さい",
                                   f"accessor {i}: the bufferView's byteStride {stride} is smaller than the element "
                                   f"size {elem}"), "truncated")
            need = (stride * (count - 1) + elem) if (stride and stride != elem) else count * elem
            if "byteLength" in bv and aoff + need > self._offset(bv, "byteLength", f"bufferView {a['bufferView']}"):
                # bufferView の範囲を越えて隣の bufferView のデータを黙って読まない（点検 R02A-03）
                raise GltfError(tr(f"accessor {i} のデータが bufferView の範囲を超えている（必要 {aoff + need} バイト、"
                                   f"bufferView {bv['byteLength']} バイト）",
                                   f"accessor {i}'s data goes beyond its bufferView ({aoff + need} bytes needed, the "
                                   f"bufferView has {bv['byteLength']} bytes)"), "truncated")
            if off + need > len(buf):
                # numpy の英語の文（buffer is smaller than requested size など）にしない
                raise GltfError(tr(f"accessor {i} のデータが足りない（ファイルが途中で切れている可能性。"
                                   f"必要 {off + need} バイト、バッファ {len(buf)} バイト）",
                                   f"accessor {i} has too little data (the file may be cut off; {off + need} bytes "
                                   f"needed, the buffer has {len(buf)} bytes)"), "truncated")
            if stride and stride != elem:
                raw = np.frombuffer(buf, dtype=np.uint8, count=stride * (count - 1) + elem, offset=off)
                rows = np.lib.stride_tricks.as_strided(raw, shape=(count, elem), strides=(stride, 1))
                out = np.ascontiguousarray(rows).view(dtype).reshape(count, ncomp)
            else:
                out = np.frombuffer(buf, dtype=dtype, count=count * ncomp, offset=off).reshape(count, ncomp)
        if a.get("normalized") and dtype.kind in "iu":
            out = np.maximum(out.astype(np.float64) / _NORM_DIV[dtype.type], -1.0)
        return out

    # -- 画像 --
    def image(self, i: int) -> tuple[np.ndarray, str]:
        """画像 i（(h, w, 4) uint8, mime）。同じ画像を使う複数のマテリアルで復号し直さず、同じ配列を返す
        （1 枚のアトラスをマテリアルの数だけ復号して別々に持っていた。点検 R02B-02）。images の別の要素でも、データの
        出どころ（bufferView の番号か uri）が同じなら 1 回だけ復号して配列を共有する。復号する画像の画素数の合計が
        :data:`_MAX_IMAGE_PIXELS` を超えたら GltfError（reason ``images_too_large``。頂点・ノードの上限の ``too_large``
        とは直し方（テクスチャの枚数・大きさを減らす）が違うので分ける。2026-11-04 点検 D1_lib-03）にする（R02B-02 の見直し）。"""
        from PIL import Image
        im = self._item("images", i)
        if i in self._images:
            return self._images[i]
        if "bufferView" in im:
            self._item("bufferViews", im["bufferView"])     # 番号を確かめてから鍵にする（リストなどで TypeError にしない）
            key = ("bufferView", im["bufferView"])
        elif "uri" in im:
            if not isinstance(im["uri"], str):
                raise GltfError(tr(f"image {i} の uri が文字列ではない: {im['uri']!r}",
                                   f"image {i}: the uri is not a string: {im['uri']!r}"), "truncated")
            key = ("uri", im["uri"])
        else:
            raise GltfError(tr(f"image {i} にデータがない", f"image {i} has no data"))
        data = self._image_data.get(key)
        if data is None:
            cut = False
            if key[0] == "bufferView":
                buf, off, _ = self.buffer_view(im["bufferView"])
                bv = self._item("bufferViews", im["bufferView"])
                n = self._offset(bv, "byteLength", f"bufferView {im['bufferView']}")
                raw = buf[off:off + n]
                cut = off + n > len(buf)       # 画像のバイト列がバッファの外まで続く = ファイルが途中で切れている
            else:
                raw = self._read_uri(im["uri"])
            try:
                # 形式を限る（GLB に埋め込まれた EPS などで、Pillow が Ghostscript などの外部のプログラムを起動しないように。
                # 点検 R10B-04）
                pil = Image.open(io.BytesIO(raw), formats=_IMAGE_FORMATS)
                # 復号（load）の前に、ヘッダーの大きさで画素数の合計を確かめる
                w, h = pil.size
                self._image_pixels += w * h
                if self._image_pixels > _MAX_IMAGE_PIXELS:
                    raise GltfError(tr(f"テクスチャの画像が大きすぎる（image {i} までの画像の画素数の合計 "
                                       f"{self._image_pixels} が上限 {_MAX_IMAGE_PIXELS} を超えた）",
                                       f"The texture images are too large (the total pixel count of the images up to "
                                       f"image {i} is {self._image_pixels}, over the limit of {_MAX_IMAGE_PIXELS})"),
                                    "images_too_large", pixels=self._image_pixels, limit=_MAX_IMAGE_PIXELS)
                pil.load()
            except GltfError:
                raise
            except Exception as e:  # noqa: BLE001
                raise GltfError(tr(f"image {i} を読めない: {e}", f"Cannot read image {i}: {e}"),
                                "truncated" if cut else None) from None
            fmt_mime = Image.MIME.get(pil.format, "") if pil.format else ""
            if pil.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
                data = _gray16_to_rgba(pil), fmt_mime
            else:
                data = np.asarray(pil.convert("RGBA"), dtype=np.uint8).copy(), fmt_mime
            self._image_data[key] = data
        out = data[0], (im.get("mimeType") or data[1])
        self._images[i] = out
        return out

    def material(self, i: int | None, cache: dict) -> Material:
        if i is None:
            return Material(name="(default)")
        md = self._item("materials", i)
        if i in cache:
            return cache[i]
        pbr = md.get("pbrMetallicRoughness", {})
        factor = pbr.get("baseColorFactor", (1, 1, 1, 1))
        tex = pbr.get("baseColorTexture")
        sg = (md.get("extensions") or {}).get("KHR_materials_pbrSpecularGlossiness")
        if isinstance(sg, dict):            # 旧式の拡張: diffuse がベースカラーの代わり（係数は線形、テクスチャは sRGB）
            factor = sg.get("diffuseFactor", (1, 1, 1, 1))
            tex = sg.get("diffuseTexture")
        mode = str(md.get("alphaMode", "OPAQUE")).upper()
        mat = Material(name=md.get("name", f"material{i}"), base_color_factor=tuple(float(x) for x in factor),
                       alpha_mode=mode if mode in ("OPAQUE", "MASK", "BLEND") else "OPAQUE",
                       alpha_cutoff=float(md.get("alphaCutoff", 0.5)))
        if tex is not None:
            tt = (tex.get("extensions") or {}).get("KHR_texture_transform")
            mat.texcoord = int(tex.get("texCoord", 0))
            if isinstance(tt, dict):
                if "texCoord" in tt:        # 拡張の texCoord は textureInfo の texCoord より優先
                    mat.texcoord = int(tt["texCoord"])
                M = uv_transform_matrix(tt)
                if not np.allclose(M, np.eye(3)):
                    mat.uv_transform = M
            t = self._item("textures", tex.get("index"))
            src = t.get("source")
            if src is None:
                for name in _IMAGE_EXT:
                    ext = (t.get("extensions") or {}).get(name)
                    if isinstance(ext, dict) and "source" in ext:
                        src = ext["source"]
                        break
            if src is not None:
                mat.image, mat.image_mime = self.image(src)
            else:
                self.warn(tr(f"material {i}: テクスチャの画像が見つからない（読める形式の画像が無い）",
                             f"material {i}: the texture image was not found (no image in a readable format)"),
                          f"load_no_image:material {i}")
            sampler = self._item("samplers", t["sampler"]) if "sampler" in t else {}
            mat.wrap = (int(sampler.get("wrapS", _REPEAT)), int(sampler.get("wrapT", _REPEAT)))
        a = mat.base_color_factor[3]
        if mat.alpha_mode == "MASK":
            cut = mat.alpha_cutoff * 255.0
            if a * 255.0 < cut or (mat.has_texture and (mat.image[..., 3].astype(np.float64) * a < cut).any()):
                self.warn(tr(f"material {i}: 切り抜き（alphaMode MASK）は使えないので、不透明として変換した"
                             "（切り抜かれる部分は近くの色で塗った）",
                             f"material {i}: cutouts (alphaMode MASK) are not supported, so it was converted as opaque "
                             "(the cut-out parts were painted with nearby colors)"), f"load_alpha_mask:material {i}")
        elif mat.alpha_mode == "BLEND":
            if a < 1.0 or (mat.has_texture and (mat.image[..., 3] < 255).any()):
                self.warn(tr(f"material {i}: 半透明（alphaMode BLEND）は使えないので、不透明として変換した",
                             f"material {i}: semi-transparency (alphaMode BLEND) is not supported, so it was "
                             "converted as opaque"),
                          f"load_alpha_blend:material {i}")
        cache[i] = mat
        return mat

    # -- ノード --
    @staticmethod
    def _node_vec(node: dict, key: str, n: int, default) -> np.ndarray:
        """ノードの matrix・translation・rotation・scale（n 個の有限の数）。違えば GltfError（点検 R02A-04）。"""
        v = node.get(key, default)
        if (not isinstance(v, (list, tuple)) or len(v) != n
                or not all(isinstance(x, numbers.Real) and not isinstance(x, bool) for x in v)
                or not np.isfinite(np.asarray(v, dtype=np.float64)).all()):
            raise GltfError(tr(f"ノードの {key} が不正（{n} 個の有限の数）: {v!r}",
                               f"The node's {key} is invalid ({n} finite numbers): {v!r}"), "truncated")
        return np.asarray(v, dtype=np.float64)

    @staticmethod
    def node_matrix(node: dict) -> np.ndarray:
        """ノードの局所行列。回転の四元数は単位長でなければ長さで割ってから使う（長さ 0 は回転なし。単位長でない値で
        拡大と別の角度の行列にしない。点検 R02B-05）。"""
        if "matrix" in node:
            return _Reader._node_vec(node, "matrix", 16, None).reshape(4, 4).T   # 列優先 → 通常の行列
        t = _Reader._node_vec(node, "translation", 3, (0, 0, 0))
        q = _Reader._node_vec(node, "rotation", 4, (0, 0, 0, 1))
        s = _Reader._node_vec(node, "scale", 3, (1, 1, 1))
        qn = float(np.linalg.norm(q))
        if abs(qn - 1.0) > 1e-6:          # float32 の丸め程度の値はそのまま（結果を変えない）
            q = q / qn if qn > 1e-8 else np.array([0.0, 0.0, 0.0, 1.0])
        x, y, z, w = (float(v) for v in q)
        r = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ])
        m = np.eye(4)
        m[:3, :3] = r * s[None, :]
        m[:3, 3] = t
        return m

    def _local(self, ni: int, node: dict) -> np.ndarray:
        """:meth:`node_matrix`。単位長でない回転の四元数（仕様違反。正規化して使う）は注意を記録に残す。"""
        m = self.node_matrix(node)
        if "matrix" not in node and "rotation" in node:
            qn = float(np.linalg.norm(np.asarray(node["rotation"], dtype=np.float64)))
            if abs(qn - 1.0) > 1e-3:
                self.warn(tr(f"node {ni}: 回転の四元数の長さが 1 ではない（{qn:.4g}。仕様違反。正規化して使った）",
                             f"node {ni}: the rotation quaternion's length is not 1 ({qn:.4g}; against the "
                             "specification; normalized)"))
        return m

    def _scene_instances(self) -> tuple[list[tuple[int, np.ndarray, int]], dict[int, np.ndarray]]:
        """シーンのノードを巡り、(メッシュ, ワールド行列, ノード) の一覧と {ノード: ワールド行列} を返す。

        明示的なスタックによる深さ優先の前順（再帰の上限も段数の上限も無い）。子が今の祖先の中に現れたときだけ
        循環とみなす。親が 2 つあるノード（仕様違反）は、今までどおりそれぞれの位置に置き、注意だけにする。
        ただし、重複の展開は深さに対して指数的に増えうる（各ノードが同じ子を 2 回持つ鎖）ので、ノードの訪問回数が
        max(4 × ノード数, :data:`_MAX_NODE_VISITS`) を超えたら GltfError（reason ``too_large``）にする（正しい木では
        訪問回数 ≤ ノード数。点検 R02B-01）。"""
        doc = self.doc
        nodes = doc.get("nodes", [])
        if not isinstance(nodes, list):
            raise GltfError(tr("nodes が配列ではない", "nodes is not an array"), "truncated")
        scenes = doc.get("scenes")
        if scenes:
            si = doc.get("scene", 0)
            if isinstance(si, bool) or not isinstance(si, int) or not 0 <= si < len(scenes):
                # 黙ってノードの変換を全部捨てない（three.js の scene || 0 と同じく最初のシーン。点検 R02B-05）
                self.warn(tr(f"scene の番号 {si!r} が不正なので、最初のシーンを使った",
                             f"The scene index {si!r} is invalid, so the first scene was used"))
                si = 0
            roots = self._item("scenes", si).get("nodes", [])
        else:
            children = {c for n in nodes for c in n.get("children", [])}
            roots = [i for i in range(len(nodes)) if i not in children]
        instances: list[tuple[int, np.ndarray, int]] = []
        world: dict[int, np.ndarray] = {}
        on_path: set[int] = set()
        stack: list[tuple[int, np.ndarray | None, bool]] = [(r, np.eye(4), False) for r in reversed(roots)]
        limit = max(4 * len(nodes), _MAX_NODE_VISITS)
        visits = 0
        while stack:
            ni, pm, leaving = stack.pop()
            if leaving:
                on_path.discard(ni)
                continue
            if isinstance(ni, bool) or not isinstance(ni, int) or not 0 <= ni < len(nodes):
                raise GltfError(tr(f"ノードの番号が範囲外: {ni}", f"Node index out of range: {ni}"), "truncated")
            if ni in on_path:
                raise GltfError(tr(f"ノード階層に循環がある（node {ni}）", f"The node hierarchy has a cycle (node {ni})"),
                                "truncated")
            visits += 1
            if visits > limit:
                raise GltfError(tr(f"ノード階層の展開が多すぎる（親が複数あるノードの重複で、ノードの訪問が上限 {limit} 回を"
                                   "超えた）",
                                   f"The node hierarchy expands too much (nodes with more than one parent made the "
                                   f"node visits exceed the limit of {limit})"), "too_large", limit=limit)
            node = nodes[ni]
            if not isinstance(node, dict):
                raise GltfError(tr(f"node {ni} がオブジェクトではない", f"node {ni} is not an object"), "truncated")
            m = pm @ self._local(ni, node)
            if ni in world:
                self.warn(tr(f"node {ni} に親が複数ある（仕様違反。それぞれの位置に置いた）",
                             f"node {ni} has more than one parent (against the specification; placed at each position)"))
            else:
                world[ni] = m
            if "mesh" in node:
                instances.append((node["mesh"], m, ni))
            on_path.add(ni)
            stack.append((ni, None, True))
            for c in reversed(node.get("children", [])):
                stack.append((c, m, False))
        return instances, world

    def _node_world(self, ni: int, world: dict[int, np.ndarray]) -> np.ndarray:
        """ノードのワールド行列（シーンの巡回で求めていなければ、親をたどって求める。ジョイント用）。"""
        if ni in world:
            return world[ni]
        nodes = self.doc.get("nodes", [])
        parents = {c: p for p, n in enumerate(nodes) for c in n.get("children", [])}
        chain, seen, k = [], set(), ni
        while k is not None and k not in world:
            if not isinstance(k, int) or not 0 <= k < len(nodes):
                raise GltfError(tr(f"ノードの番号が範囲外: {k}", f"Node index out of range: {k}"), "truncated")
            if k in seen:
                raise GltfError(tr(f"ノード階層に循環がある（node {k}）", f"The node hierarchy has a cycle (node {k})"),
                                "truncated")
            seen.add(k)
            chain.append(k)
            k = parents.get(k)
        m = world[k] if k is not None else np.eye(4)
        for k2 in reversed(chain):
            m = m @ self._local(k2, nodes[k2])
            world[k2] = m
        return world[ni]

    def _joint_matrices(self, si, world: dict[int, np.ndarray], cache: dict) -> np.ndarray | None:
        """スキン si の各ジョイントの「ワールド変換 × inverseBindMatrix」(nj, 4, 4)。スキンが不正なら None。"""
        if si in cache:
            return cache[si]
        skins = self.doc.get("skins", [])
        out = None
        if isinstance(si, int) and 0 <= si < len(skins) and skins[si].get("joints"):
            skin = skins[si]
            joints = skin["joints"]
            if "inverseBindMatrices" in skin:
                ibm = self.accessor(skin["inverseBindMatrices"]).astype(np.float64)
                # 仕様: IBM の個数はジョイント数以上（多い分は使わない）
                if ibm.ndim == 2 and ibm.shape[1] == 16 and len(ibm) >= len(joints):
                    ibm = ibm[:len(joints)].reshape(-1, 4, 4).transpose(0, 2, 1)       # 列優先 → 通常の行列
                else:
                    ibm = None
            else:
                ibm = np.repeat(np.eye(4)[None], len(joints), axis=0)
            if ibm is not None:
                out = np.stack([self._node_world(int(j), world) @ ibm[k] for k, j in enumerate(joints)])
        cache[si] = out
        return out

    def _skinned_matrices(self, attrs: dict, n: int, J: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
        """頂点ごとの静止形の行列 Σ w_k・J[j_k] (n, 4, 4) と、ウェイトの和が 0 でない頂点の印。

        JOINTS_n / WEIGHTS_n が無ければ None。"""
        sets = []
        k = 0
        while f"JOINTS_{k}" in attrs and f"WEIGHTS_{k}" in attrs:
            # 数の不一致は配列を作る前に止める（データの無い巨大な count で先に確保しないように。点検 R02A-01）
            if self._count(attrs[f"JOINTS_{k}"]) != n or self._count(attrs[f"WEIGHTS_{k}"]) != n:
                raise GltfError(tr("スキンの JOINTS / WEIGHTS の数が頂点数と合わない",
                                   "The skin's JOINTS / WEIGHTS counts do not match the vertex count"))
            sets.append((self.accessor(attrs[f"JOINTS_{k}"]), self.accessor(attrs[f"WEIGHTS_{k}"])))
            k += 1
        if not sets:
            return None
        jidx = np.hstack([j for j, _ in sets]).astype(np.int64)
        w = np.hstack([x for _, x in sets]).astype(np.float64)
        if jidx.shape != w.shape or jidx.shape[0] != n:
            raise GltfError(tr("スキンの JOINTS / WEIGHTS の数が頂点数と合わない",
                               "The skin's JOINTS / WEIGHTS counts do not match the vertex count"))
        if len(jidx) and (jidx.min() < 0 or jidx.max() >= len(J)):
            raise GltfError(tr("スキンのジョイントの番号が範囲外", "Skin joint index out of range"))
        s = w.sum(axis=1)
        good = s > 1e-8
        off = good & (np.abs(s - 1.0) > 0.01)
        if off.any():
            self.warn(tr(f"ウェイトの和が 1 でない頂点 {int(off.sum())} 個を正規化した",
                         f"Normalized the vertices whose weights did not sum to 1: {int(off.sum())}"))
        w[good] /= s[good, None]
        Mv = np.zeros((n, 4, 4))
        for c in range(w.shape[1]):
            Mv += w[:, c, None, None] * J[jidx[:, c]]
        return Mv, good

    def model(self) -> Model:
        doc = self.doc
        if not doc.get("meshes"):
            raise GltfError(tr("メッシュがない", "No mesh"))
        instances, world = self._scene_instances()
        if not instances:   # ノードの無いファイルはメッシュをそのまま使う
            instances = [(i, np.eye(4), None) for i in range(len(doc["meshes"]))]

        P, UV, C, F, FM = [], [], [], [], []
        mats: list[Material] = []
        mat_index: dict[int | None, int] = {}
        cache: dict = {}
        skin_cache: dict = {}
        has_uv = has_col = False
        base = 0
        prims = []
        n_vert = n_idx = n_idx_data = 0
        pos_acc: set[int] = set()
        idx_acc: set[int] = set()
        for mesh_i, mtx, ni in instances:
            for prim in self._item("meshes", mesh_i).get("primitives", []):
                # ノード × プリミティブの数は JSON の大きさにほぼ比例して増やせ、頂点 0 個のプリミティブは頂点の合計にも
                # 足されないので、積む前に数を確かめる（2026-10-29 G02 の見直し）
                if len(prims) >= _MAX_PRIM_INSTANCES:
                    raise GltfError(tr(f"モデルが大きすぎる（シーンに置くプリミティブが上限 {_MAX_PRIM_INSTANCES} 個を超えた）",
                                       f"The model is too large (the primitives placed in the scene exceed the limit "
                                       f"of {_MAX_PRIM_INSTANCES})"), "too_large", limit=_MAX_PRIM_INSTANCES)
                attrs = prim.get("attributes", {})
                prims.append((prim, mtx, ni))
                if prim.get("mode", 4) in (4, 5, 6) and "POSITION" in attrs:
                    mat = self.material(prim.get("material"), cache)
                    has_uv |= self._uv_attr(mat) in attrs
                    c = self._count(attrs["POSITION"])
                    n_vert += c
                    pos_acc.add(attrs["POSITION"])
                    if "indices" in prim:
                        k = self._count(prim["indices"])
                        n_idx += k
                        n_idx_data += k
                        idx_acc.add(prim["indices"])
                    else:
                        n_idx += c
                    # 置く頂点の合計を、データを読む前に JSON の数だけで、足すたびに確かめる（同じメッシュを多数のノードで
                    # 使い回す GLB やデータの無い巨大な count で、数百 KB のファイルが数 GB を確保していた。点検 R02A-01・
                    # R02B-01）
                    if n_vert > _MAX_LOAD_VERTICES or n_idx > 6 * _MAX_LOAD_VERTICES:
                        raise GltfError(tr(f"モデルが大きすぎる（シーンに置く頂点 {n_vert} 個・インデックス {n_idx} 個。"
                                           f"頂点の上限 {_MAX_LOAD_VERTICES} 個）",
                                           f"The model is too large ({n_vert} vertices and {n_idx} indices placed in the "
                                           f"scene; the vertex limit is {_MAX_LOAD_VERTICES})"),
                                        "too_large", vertices=n_vert, limit=_MAX_LOAD_VERTICES)
                has_col |= "COLOR_0" in attrs
        # 上限の内でも、同じデータの使い回しで増やした分は小さな上限にする（1 万頂点のメッシュを 2,000 ノードで置く
        # 44 万バイトの GLB が 2,000 万頂点で数 GB〜十数 GB を確保していた）。使い回しの無いモデルは、データの
        # バイト数がそのまま上限を広げるので変わらない（2026-10-29 G02 の見直し）
        if n_vert > _MAX_REUSED_VERTICES or n_idx_data > 6 * _MAX_REUSED_VERTICES:
            limit = _MAX_REUSED_VERTICES + self._view_bytes(pos_acc) // 3
            if n_vert > limit or n_idx_data > 6 * _MAX_REUSED_VERTICES + self._view_bytes(idx_acc):
                raise GltfError(tr(f"モデルが大きすぎる（同じデータの使い回しで、シーンに置く頂点が {n_vert} 個・"
                                   f"インデックスが {n_idx} 個になる。データから読める頂点の上限 {limit} 個）",
                                   f"The model is too large (reusing the same data places {n_vert} vertices and "
                                   f"{n_idx} indices in the scene; the vertex limit for its data is {limit})"),
                                "too_large", vertices=n_vert, limit=limit)
        for prim, mtx, ni in prims:
            mode = prim.get("mode", 4)
            if mode not in (4, 5, 6):
                self.warn(tr(f"三角形以外のプリミティブ（mode {mode}）を無視した",
                             f"Ignored primitives that are not triangles (mode {mode})"),
                          f"load_non_triangle:mode {mode}")
                continue
            if prim.get("targets"):
                self.warn(tr("モーフターゲットは無視した（基本の形だけを使った）",
                             "Morph targets were ignored (only the base shape was used)"), "load_morph")
            attrs = prim["attributes"]
            if "POSITION" not in attrs:
                continue
            self._check_type(attrs["POSITION"], "POSITION", ("VEC3",))
            pos = self.accessor(attrs["POSITION"]).astype(np.float64)
            n = len(pos)
            if "indices" in prim:
                # 浮動小数点などのインデックスを黙って切り捨てて使わない（点検 R02A-03）
                self._check_type(prim["indices"], "indices", ("SCALAR",), (5121, 5123, 5125))
                idx = self.accessor(prim["indices"]).reshape(-1).astype(np.int64)
            else:
                idx = np.arange(n, dtype=np.int64)
            tri = _triangulate(idx, mode)
            if len(tri) and (tri.min() < 0 or tri.max() >= n):
                raise GltfError(tr("インデックスが範囲外", "Index out of range"))
            node = doc["nodes"][ni] if ni is not None else {}
            skinned = None
            if "skin" in node:
                # 仕様: スキン付きのメッシュはノード（と親）の変換を使わず、ジョイントの変換だけで形を決める
                J = self._joint_matrices(node["skin"], world, skin_cache)
                skinned = self._skinned_matrices(attrs, n, J) if J is not None else None
                if skinned is None:
                    self.warn(tr(f"node {ni}: スキンの情報が欠けているので、ノードの変換で形を読んだ",
                                 f"node {ni}: the skin information is incomplete, so the shape was read with the node "
                                 "transform"),
                              f"load_skin_incomplete:node {ni}")
                else:
                    self.warn(tr(f"node {ni}: 元のスキン（骨格・ウェイト）は使わず、静止姿勢の形だけを読んだ",
                                 f"node {ni}: the original skin (skeleton and weights) is not used; only the rest pose "
                                 "shape was read"))
            if skinned is None:
                pos = pos @ mtx[:3, :3].T + mtx[:3, 3]
                if np.linalg.det(mtx[:3, :3]) < 0:     # 鏡像のノードは巻き順が反転する
                    tri = tri[:, [0, 2, 1]]
            else:
                Mv, good = skinned
                Mv[~good] = mtx                        # ウェイトの無い頂点はノードの変換
                pos = np.einsum("nij,nj->ni", Mv[:, :3, :3], pos) + Mv[:, :3, 3]
                neg = np.linalg.det(Mv[:, :3, :3]) < 0
                if len(tri) and neg.any():             # 鏡像の行列の頂点が多い面は巻き順を入れ替える
                    flip = neg[tri].sum(axis=1) >= 2
                    tri[flip] = tri[flip][:, [0, 2, 1]]
            key = prim.get("material")
            mat = self.material(key, cache)
            uv_attr = self._uv_attr(mat)
            if uv_attr in attrs:
                # 数が違うと、後のプリミティブの UV がずれるか、後段で範囲外になる。配列を作る前に JSON の数で比べる
                # （データの無い巨大な count で先に確保しないように。点検 R02A-01）
                nuv = self._count(attrs[uv_attr])
                if nuv != n:
                    raise GltfError(tr(f"{uv_attr} の数（{nuv}）が POSITION の数（{n}）と合わない",
                                       f"The {uv_attr} count ({nuv}) does not match the POSITION count ({n})"),
                                    "truncated")
                self._check_type(attrs[uv_attr], uv_attr, ("VEC2", "VEC3", "VEC4"))
                uv = self.accessor(attrs[uv_attr]).astype(np.float64)[:, :2]
            else:
                uv = np.zeros((n, 2))
                if mat.has_texture:
                    self.warn(tr(f"material {key}: テクスチャの {uv_attr} が無いプリミティブがある（色がずれる）",
                                 f"material {key}: some primitives have no {uv_attr} for the texture (the colors will "
                                 "be off)"),
                              f"load_texcoord_missing:material {key}")
            if mat.has_texture and mat.uv_transform is not None:     # KHR_texture_transform
                uv = uv @ mat.uv_transform[:2, :2].T + mat.uv_transform[:2, 2]
            if mat.has_texture:
                # 数値でない UV は texture.needs_bake で焼き直しに回る（その画素は近くの色で塗る）。利用者に知らせる
                # （点検 R04B-01）
                nf = int((~np.isfinite(uv).all(axis=1)).sum())
                if nf:
                    self.warn(tr(f"material {key}: テクスチャの UV が数値でない（NaN・inf）頂点が {nf} 個ある"
                                 "（焼き直しで 1 枚のテクスチャにした）",
                                 f"material {key}: {nf} {'vertex has' if nf == 1 else 'vertices have'} texture UVs that are not numbers (NaN or inf) "
                                 "(rebaked into a single texture)"),
                              f"load_uv_nonfinite:material {key}")
            if mat.has_texture and mat.wrap != (_REPEAT, _REPEAT) and len(uv) and (uv.min() < 0 or uv.max() > 1):
                self.warn(tr(f"material {key}: テクスチャの繰り返し方法（{mat.wrap}）は未対応なので、繰り返し（REPEAT）"
                             "として扱った",
                             f"material {key}: the texture wrap mode ({mat.wrap}) is not supported, so it was treated "
                             "as repeat (REPEAT)"), f"load_sampler:material {key}")
            if "COLOR_0" in attrs:
                ncol = self._count(attrs["COLOR_0"])
                if ncol != n:
                    raise GltfError(tr(f"COLOR_0 の数（{ncol}）が POSITION の数（{n}）と合わない",
                                       f"The COLOR_0 count ({ncol}) does not match the POSITION count ({n})"),
                                    "truncated")
                # SCALAR などは texture.py の合成で IndexError（EXPORT_FAILED の的外れな案内）になっていた
                self._check_type(attrs["COLOR_0"], "COLOR_0", ("VEC3", "VEC4"))
                col = self.accessor(attrs["COLOR_0"]).astype(np.float32)
                if col.shape[1] == 3:
                    col = np.hstack([col, np.ones((n, 1), np.float32)])
                nf = int((~np.isfinite(col).all(axis=1)).sum())
                if nf:                      # 焼き直しで、その画素は近くの色で塗る（点検 R04B-01）
                    self.warn(tr(f"material {key}: 頂点色（COLOR_0）が数値でない（NaN・inf）頂点が {nf} 個ある"
                                 "（その部分は近くの色で塗った）",
                                 f"material {key}: {nf} {'vertex has' if nf == 1 else 'vertices have'} vertex colors (COLOR_0) that are not numbers "
                                 "(NaN or inf) (those parts were painted with nearby colors)"),
                              f"load_color_nonfinite:material {key}")
            else:
                col = np.ones((n, 4), np.float32)
            if key not in mat_index:
                mat_index[key] = len(mats)
                mats.append(mat)
            P.append(pos)
            UV.append(uv)
            C.append(col)
            F.append(tri + base)
            FM.append(np.full(len(tri), mat_index[key], dtype=np.int64))
            base += n
        if not P:
            raise GltfError(tr("三角形メッシュがない", "No triangle mesh"), "no_triangles")
        model = Model(
            positions=np.vstack(P), faces=np.vstack(F),
            uvs=np.vstack(UV) if has_uv else None,
            colors=np.vstack(C).astype(np.float32) if has_col else None,
            face_material=np.concatenate(FM), materials=mats, warnings=self.warnings, notices=self.notices,
        )
        # 未使用頂点は残す（後段の溶接・間引きで消える）。退化三角形だけ除く（画面には出さない）
        f = model.faces
        ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
        if not ok.all():
            model.warnings.append(tr(f"退化三角形 {int((~ok).sum())} 個を除いた",
                                     f"Removed degenerate triangles: {int((~ok).sum())}"))
            model.faces = f[ok]
            model.face_material = model.face_material[ok]
        if model.face_count == 0:
            raise GltfError(tr("有効な三角形がない（退化した面だけ）", "No valid triangles (only degenerate faces)"),
                            "no_triangles")
        return model

    @staticmethod
    def _uv_attr(mat: Material) -> str:
        """プリミティブの UV に使う属性。テクスチャがあればその texCoord、無ければ TEXCOORD_0。"""
        return f"TEXCOORD_{mat.texcoord}" if mat.has_texture else "TEXCOORD_0"


def _gray16_to_rgba(pil) -> np.ndarray:
    """16 bit のグレースケール（Pillow の I;16 系・I）→ RGBA の 8 bit。

    ``convert('RGBA')``・``convert('L')`` は 255 を超える値を 255 に切り詰めて白く飽和するので使わず、上位バイトを取る
    （Pillow が 16 bit の RGB を読むときと同じ）。tRNS（16 bit の値）は縮める前に比べて、その画素のアルファを 0 にする。
    """
    a16 = np.clip(np.asarray(pil).astype(np.int64), 0, 65535)
    g8 = (a16 >> 8).astype(np.uint8)
    alpha = np.full(g8.shape, 255, dtype=np.uint8)
    t = pil.info.get("transparency")
    if isinstance(t, int):
        alpha[a16 == t] = 0
    return np.ascontiguousarray(np.stack([g8, g8, g8, alpha], axis=-1))


def _triangulate(idx: np.ndarray, mode: int) -> np.ndarray:
    if mode == 4:
        return idx[: len(idx) // 3 * 3].reshape(-1, 3)
    if mode == 5:   # strip
        tris = []
        for i in range(len(idx) - 2):
            a, b, c = idx[i], idx[i + 1], idx[i + 2]
            tris.append((a, b, c) if i % 2 == 0 else (b, a, c))
        return np.array(tris, dtype=np.int64).reshape(-1, 3)
    # fan
    return np.array([(idx[0], idx[i], idx[i + 1]) for i in range(1, len(idx) - 1)], dtype=np.int64).reshape(-1, 3)


# ----------------------------------------------------------------------
# 書き出し（確認用とテスト用の最小限。POSITION/NORMAL/TEXCOORD_0 + baseColor 1 枚）
# ----------------------------------------------------------------------
def save_glb(path: str, positions, faces, uvs=None, image: np.ndarray | None = None, normals=None,
             colors=None) -> None:
    """三角形メッシュを GLB で書く。``faces`` は反時計回り。``image`` は (h,w,3|4) uint8。"""
    positions = np.ascontiguousarray(positions, dtype="<f4")
    faces = np.ascontiguousarray(faces, dtype="<u4")
    chunks: list[bytes] = []
    views: list[dict] = []
    accessors: list[dict] = []

    def add_view(raw: bytes, target: int | None = None) -> int:
        off = sum(len(c) for c in chunks)
        chunks.append(raw + b"\x00" * ((4 - len(raw) % 4) % 4))
        v = {"buffer": 0, "byteOffset": off, "byteLength": len(raw)}
        if target:
            v["target"] = target
        views.append(v)
        return len(views) - 1

    def add_accessor(arr: np.ndarray, typ: str, ctype: int, minmax: bool = False) -> int:
        a = {"bufferView": add_view(arr.tobytes(), 34962 if typ != "SCALAR" else 34963),
             "componentType": ctype, "count": int(len(arr)), "type": typ}
        if minmax:
            a["min"] = arr.min(axis=0).tolist()
            a["max"] = arr.max(axis=0).tolist()
        accessors.append(a)
        return len(accessors) - 1

    attrs = {"POSITION": add_accessor(positions, "VEC3", 5126, True)}
    if normals is not None:
        attrs["NORMAL"] = add_accessor(np.ascontiguousarray(normals, dtype="<f4"), "VEC3", 5126)
    if uvs is not None:
        attrs["TEXCOORD_0"] = add_accessor(np.ascontiguousarray(uvs, dtype="<f4"), "VEC2", 5126)
    if colors is not None:
        attrs["COLOR_0"] = add_accessor(np.ascontiguousarray(colors, dtype="<f4"), "VEC4", 5126)
    ind = add_accessor(faces.reshape(-1), "SCALAR", 5125)
    doc: dict = {
        "asset": {"version": "2.0", "generator": "gdconvert"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": attrs, "indices": ind, "mode": 4, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0.0,
                                                "roughnessFactor": 1.0}}],
    }
    if image is not None:
        from PIL import Image
        bio = io.BytesIO()
        Image.fromarray(np.ascontiguousarray(image)).save(bio, format="PNG")
        iv = add_view(bio.getvalue())
        doc["images"] = [{"bufferView": iv, "mimeType": "image/png"}]
        doc["textures"] = [{"source": 0}]
        doc["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
    doc["accessors"] = accessors
    doc["bufferViews"] = views
    binary = b"".join(chunks)
    doc["buffers"] = [{"byteLength": len(binary)}]
    js = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    js += b" " * ((4 - len(js) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(binary)
    with open(path, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, total))
        f.write(struct.pack("<I4s", len(js), b"JSON") + js)
        f.write(struct.pack("<I4s", len(binary), b"BIN\x00") + binary)
