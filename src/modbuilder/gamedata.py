"""ゲームのインストール先を読み取り専用で参照する。

* 公式レコード: 各 .arz（本編・gdx1〜3）を :class:`modbuilder.arz.ArzFile` で直接読む（ArchiveTool での 1.3 GB の
  展開は不要。展開結果とのテキスト一致は arz.py の冒頭を参照）。
* テンプレート: ``database/templates.arc`` を :class:`modbuilder.arc.ArcFile` で読み、キャッシュに書き出す。
* 公式ファイルの一覧: ``resources/*.arc``（本編・gdx1〜3）のファイル表。
* 公式タグ: 各 ``Text_EN.arc`` のタグファイル。
* プレイヤー骨格: ``creatures/pc/hero_00.msh`` / ``heroine_00.msh``（gdconvert の refs にあればそれを使う）。

ゲームのファイルは ``open(..., "rb")`` で読むだけで、書き込み・ArchiveTool の実行はしない（更新日時も変えない）。
テストでは同じ形のメソッドを持つ偽物（tests/test_modbuilder.py の FakeGameData）を使う。
"""
from __future__ import annotations

import hashlib
import json
import os

from . import paths
from ._i18n import tr
from .arc import ArcFile
from .arz import ArzFile, TemplateSet, norm_path
from .errors import GameDataError
from .naming import DB_PRIORITY, PLAYER_SKELETONS

__all__ = ["GameData", "DATABASES", "RESOURCE_DIRS", "GAME_TOOLS", "looks_like_gd", "looks_like_install_target",
           "missing_game_tools", "parse_tags"]

DATABASES = {"base": "database/database.arz", "gdx1": "gdx1/database/GDX1.arz",
             "gdx2": "gdx2/database/GDX2.arz", "gdx3": "gdx3/database/GDX3.arz"}
RESOURCE_DIRS = {"base": "resources", "gdx1": "gdx1/resources", "gdx2": "gdx2/resources", "gdx3": "gdx3/resources"}
TEMPLATES_ARC = "database/templates.arc"
GAME_TOOLS = ("ArchiveTool.exe", "TextureCompiler.exe")     # ゲームに付属するツール（MOD の作成・変換に使う）


def looks_like_gd(path: str) -> bool:
    """Grim Dawn のインストール先か（database\\database.arz があるか）。付属のツール（ArchiveTool.exe・
    TextureCompiler.exe）は別に :func:`missing_game_tools` で確かめる（ツールが無いだけでゲーム自体が見つからない扱いに
    すると、どのファイルが欠けているかが分からない。2026-10-17 点検 C82）。"""
    return bool(path) and os.path.isfile(os.path.join(path, "database", "database.arz"))


def looks_like_install_target(path: str) -> bool:
    """MOD の導入先として使えるか（database\\database.arz と ArchiveTool.exe がある。テスト用の導入先は中身が空の
    ファイルでよい）。導入先の判定は従来どおりこの 2 つで行う（点検 C82 でゲームの判定と分けた）。"""
    return looks_like_gd(path) and os.path.isfile(os.path.join(path, "ArchiveTool.exe"))


def missing_game_tools(path: str) -> list[str]:
    """ゲームのフォルダーに無い付属のツール（:data:`GAME_TOOLS` のうち）。"""
    return [t for t in GAME_TOOLS if not os.path.isfile(os.path.join(path, t))]


def parse_tags(data: bytes, problems: list[str] | None = None, where: str = "") -> dict[str, str]:
    """タグファイル（key=value、# はコメント）を辞書に。problems を渡すと書式の問題を追加する。"""
    tags: dict[str, str] = {}
    text = data.decode("utf-8-sig", "replace")
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            if problems is not None:
                problems.append(tr(f"{where}:{i}: '=' がありません: {line!r}", f"{where}:{i}: missing '=': {line!r}"))
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if problems is not None and k in tags:
            problems.append(tr(f"{where}:{i}: タグ {k} が重複", f"{where}:{i}: duplicate tag {k}"))
        tags[k] = v
    return tags


class GameData:
    """Grim Dawn のインストール先（読み取り専用）。重い読み込みは最初に使うときに行う。"""

    def __init__(self, gd: str, cache_dir: str | None = None, refs_dir: str | None = None):
        if not looks_like_gd(gd):
            raise GameDataError(tr(f"Grim Dawn のインストール先ではありません（database/database.arz が必要）: {gd}",
                                   f"Not a Grim Dawn install folder (database/database.arz is required): {gd}"))
        self.gd = os.path.abspath(gd)
        self.cache_dir = cache_dir or paths.cache_dir()
        self.refs_dir = refs_dir if refs_dir is not None else paths.refs_dir()
        self._arz: dict[str, ArzFile] = {}
        self._templates: TemplateSet | None = None
        self._arc_index: dict[str, str] | None = None
        self._arcs: dict[str, ArcFile] = {}
        self._tags: dict[str, str] | None = None
        self._bones: dict[str, set[str]] = {}

    # --- 公式レコード ------------------------------------------------------------------
    def database(self, key: str) -> ArzFile | None:
        if key not in self._arz:
            p = os.path.join(self.gd, DATABASES[key].replace("/", os.sep))
            self._arz[key] = ArzFile(p) if os.path.isfile(p) else None
        return self._arz[key]

    def official_record(self, rel: str, db: str | None = None) -> tuple[str, list[tuple[str, str]]] | None:
        """最優先版（db を指定すればその .arz）の公式レコード → (db, [(name, value)])。無ければ None。"""
        for key in ([db] if db else DB_PRIORITY):
            a = self.database(key)
            if a is None:
                continue
            fields = a.record_fields(rel)
            if fields is not None:
                return key, fields
        return None

    def record_filetime(self, db: str, rel: str) -> int | None:
        """公式レコードの更新時刻（FILETIME。Crate がデータベースを作った時刻）。無ければ None。"""
        a = self.database(db)
        r = a.get(norm_path(rel)) if a is not None else None
        return r.filetime if r is not None else None

    def official_record_exists(self, rel: str) -> bool:
        rel = norm_path(rel)
        return any(a is not None and a.get(rel) is not None for a in (self.database(k) for k in DB_PRIORITY))

    # --- テンプレート --------------------------------------------------------------------
    @property
    def templates(self) -> TemplateSet:
        if self._templates is None:
            arc_path = os.path.join(self.gd, TEMPLATES_ARC.replace("/", os.sep))
            st = os.stat(arc_path)
            key = hashlib.sha1(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:12]
            root = os.path.join(self.cache_dir, "templates_" + key)
            done = os.path.join(root, ".complete")
            if not os.path.isfile(done):
                arc = ArcFile(arc_path)
                for name, data in arc.read_all().items():
                    out = os.path.join(root, "templates", name.replace("/", os.sep))
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(out, "wb") as f:
                        f.write(data)
                with open(done, "w", encoding="utf-8") as f:
                    json.dump({"source": arc_path, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                               "files": len(arc.entries)}, f)
            self._templates = TemplateSet(root)
        return self._templates

    # --- 公式ファイル -----------------------------------------------------------------------
    def _resource_arcs(self):
        for key in ("base", "gdx1", "gdx2", "gdx3"):
            d = os.path.join(self.gd, RESOURCE_DIRS[key].replace("/", os.sep))
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if f.lower().endswith(".arc"):
                    yield key, f[:-4], os.path.join(d, f)

    def arc(self, path: str) -> ArcFile:
        if path not in self._arcs:
            self._arcs[path] = ArcFile(path)
        return self._arcs[path]

    @property
    def arc_index(self) -> dict[str, str]:
        """{ゲーム内パス(小文字): '<db>:<アーカイブ名>'}。例 'items/geartorso/x.msh' → 'base:Items'。"""
        if self._arc_index is None:
            index: dict[str, str] = {}
            self._arc_files: dict[str, list[tuple[int, str]]] = {}
            for key, name, p in self._resource_arcs():
                try:
                    arc = self.arc(p)
                except ValueError:
                    continue      # menu.arc は ArchiveTool でも開けない（中身なし）
                for e in arc.entries:
                    gp = name.lower() + "/" + norm_path(e)
                    index.setdefault(gp, f"{key}:{name}")
                    self._arc_files.setdefault(gp, []).append((DB_PRIORITY.index(key), p))
            self._arc_index = index
        return self._arc_index

    def official_file_exists(self, game_path: str) -> bool:
        return norm_path(game_path) in self.arc_index

    def read_official_file(self, game_path: str) -> bytes:
        """公式 .arc の中身（同じパスが複数の拡張にあれば最優先版）。"""
        gp = norm_path(game_path)
        if gp not in self.arc_index:
            raise GameDataError(tr(f"公式の .arc にありません: {game_path}", f"Not in the official .arc files: {game_path}"))
        _prio, path = min(self._arc_files[gp])
        return self.arc(path).read(gp.split("/", 1)[1])

    # --- 公式タグ -------------------------------------------------------------------------
    @property
    def official_tags(self) -> dict[str, str]:
        if self._tags is None:
            tags: dict[str, str] = {}
            for key, name, p in self._resource_arcs():
                if name.lower() != "text_en":
                    continue
                arc = self.arc(p)
                for e in arc.entries:
                    if e.lower().endswith(".txt"):
                        tags.update(parse_tags(arc.read(e)))
            self._tags = tags
        return self._tags

    # --- プレイヤー骨格 ---------------------------------------------------------------------
    def player_bones(self, gender: str) -> set[str]:
        if gender not in self._bones:
            from gdmesh import msh
            rel = PLAYER_SKELETONS[gender]
            p = os.path.join(self.refs_dir, rel.replace("/", os.sep)) if self.refs_dir else ""
            if p and os.path.isfile(p):
                m = msh.read(p, strict=True)
            else:
                m = msh.read_bytes(self.read_official_file(rel), strict=True)
            self._bones[gender] = {b.name for b in m.bones.bones}
        return self._bones[gender]

    # --- 記録用 -------------------------------------------------------------------------------
    def fingerprint(self) -> dict:
        out = {"gd": self.gd}
        for key, rel in DATABASES.items():
            p = os.path.join(self.gd, rel.replace("/", os.sep))
            if os.path.isfile(p):
                st = os.stat(p)
                out[key] = {"size": st.st_size, "mtime": int(st.st_mtime)}
        return out
