"""統合ビルド: 装備ライブラリ → 作業フォルダー → 検証 → database.arz / Text_XX.arc / Items.arc → 往復検証。

出力フォルダー（既定 ``%LOCALAPPDATA%\\GrimDawnSkinMOD\\builds\\<ビルドID>\\``）::

  staging/database/records/...      生成した .dbr（テキスト。compile の入力そのもの）
  staging/resources/Text_XX/...     表示名タグ（v1.3.1.1 の 13 言語。:data:`modbuilder.records.LANGUAGES`）
  staging/resources/Items/...       装備のアセット（.msh / .tex）
  mod/database.arz, Text_XX.arc（13 個）, Items.arc     /basemods 形式（mods/ 直下に置くもの）
  custom/<名前>/database/<名前>.arz, custom/<名前>/resources/*.arc   Custom Game 形式（指定時のみ）
  build_manifest.json               ビルドの記録（装備・レコード・ハッシュ・検証結果）
  validation_report.txt             検証の詳細

検証で問題があれば mod/ を作らずに止め、理由を manifest（status "failed"）と例外で返す。検証以外の理由（ArchiveTool の
失敗・打ち切り、ファイルの読み書きの失敗など）で止まったときも、manifest（status "failed"）と装備の状態（build_failed）に
記録してから :class:`~modbuilder.errors.BuildStopped`（ModBuilderError）にする（2026-10-17 点検 C83・FG05-R1）。
ゲームのフォルダーに ArchiveTool.exe が無ければ、ビルドのフォルダーを作る前に :class:`~modbuilder.errors.GameToolMissing`
（GameDataError）で止める（点検 C82・FG05-R2）。

決定的なビルド（2026-10-16）: mod/ のファイルは、同じ装備（内部ID・名前・作成日時・アセット・消耗品のまとめ方）と
同じゲームデータ（版・拡張の構成）と同じ版の ModBuilder（lz4）からなら、いつ・どの PC・どのフォルダーで作っても
バイト単位で同じになる。ビルドの時刻の代わりに :func:`source_filetime`（装備の作成日時と、雛形にした公式レコードの
時刻の遅い方）を、database.arz の各レコードの時刻と、.arc に入れるファイルの更新日時（ArchiveTool はこれを
ファイル表に書く）に使う。表示名は Text_*.arc にだけ入る（database.arz には入らない）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil

from . import __version__, paths
from ._i18n import tr
from .arc import ArchiveTool
from .arz import compile_records, dbr_text, filetime_from_unix, norm_path, read_dbr
from .errors import BuildStopped, GameDataError, GameToolMissing, LibraryError, ModBuilderError, ValidationFailed
from .icons import icons_needed, write_icons
from .library import Library, now_iso
from .invisible_look import write_look
from .naming import ICON_PREFIX, INVISIBLE_LOOK_PREFIX, INVISIBLE_SOURCE, MERCHANT_RECORDS, TEMPLATE_HEAD, TEMPLATE_SET
from .records import LANGUAGES, LOCALIZED_LANGUAGES, TAGS_FILE, plan_records, tags_file_text
from .validate import Report, validate_staging, verify_copies, verify_outputs

__all__ = ["build", "file_hashes", "load_manifest", "source_filetime", "CUSTOM_NAME_RE", "RESERVED_CUSTOM_NAMES"]

# 末尾は \Z（$ は末尾の改行の前にも一致し、'mymod\n' が通って custom/ への書き出しで止まった。2026-10-27 R06A-04）
CUSTOM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}\Z")
# Custom Game に使えない名前（大文字・小文字を区別せずに比べる）。mods/survivalmode は Crucible の DLC の公式のフォルダー
# （2026-10-17 監査 C87）
RESERVED_CUSTOM_NAMES = ("survivalmode",)
# Windows の予約されたデバイス名（フォルダー名に使えない。nul では database\nul.arz の写しが捨てられ、後の手順で止まった。
# R06A-04）。大文字・小文字を区別せずに比べる
WINDOWS_RESERVED_NAMES = ("con", "prn", "aux", "nul") + tuple(f"{p}{n}" for p in ("com", "lpt") for n in range(10))
MANIFEST = "build_manifest.json"
FILETIME_SECOND = 10_000_000
FILETIME_UNIX_EPOCH = 116444736000000000
# 装備の作成日時として受け付ける範囲（UTC。今の時刻に依らない固定の範囲。決定的なビルドを壊さない。R06B-01）
CREATED_AT_MIN = _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc)
CREATED_AT_MAX = _dt.datetime(3000, 1, 1, tzinfo=_dt.timezone.utc)


def tool_missing_hint() -> str:
    """ArchiveTool.exe が無いときの文に添える、戻し方の案内（表示言語の文）。"""
    return tr("（Steam のライブラリで Grim Dawn のプロパティ →「インストール済みファイル」→「ゲームファイルの整合性を確認」で戻せます）",
              ' (you can restore it in the Steam library: Grim Dawn\'s Properties → "Installed Files" → '
              '"Verify integrity of game files")')


def source_filetime(gd, equipment, sources) -> int:
    """決定的なビルドの時刻（FILETIME、秒単位に切り上げ）: 装備の ``created_at`` と、雛形にした公式レコード
    （``sources`` の '<db>:<レコード>'）の時刻のうち最も遅いもの。ビルドした時刻・PC・フォルダーには依らない。

    公式レコードの時刻も入れるのは、上書きするレコードの時刻が公式のものより古くならないようにするため
    （これまでのゲーム内の確認は全て「MOD の方が新しい」状態だった。ゲームが時刻を使うかは未確認）。"""
    times = []
    for e in equipment:
        try:
            t = _dt.datetime.fromisoformat(e.created_at)
            if t.tzinfo is None:
                t = t.replace(tzinfo=_dt.timezone.utc)
            times.append(filetime_from_unix(t.timestamp()))
        except (TypeError, ValueError):
            pass
    rec_time = getattr(gd, "record_filetime", None)
    if rec_time is not None:
        for src in sorted(set(sources)):
            db, _, rel = src.partition(":")
            ft = rec_time(db, rel)
            if ft:
                times.append(ft)
    if not times:
        return FILETIME_UNIX_EPOCH
    ft = max(times)
    return ft + (-ft % FILETIME_SECOND)


def _set_tree_mtime(root: str, filetime: int) -> None:
    """root の下の全ファイルの更新日時を filetime にする（ArchiveTool は .arc のファイル表に更新日時を書く）。"""
    ns = (filetime - FILETIME_UNIX_EPOCH) * 100
    for dirpath, _d, files in os.walk(root):
        for f in files:
            os.utime(os.path.join(dirpath, f), ns=(ns, ns))


def file_hashes(path: str) -> dict:
    h1, h5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h1.update(block)
            h5.update(block)
    return {"size": os.path.getsize(path), "sha256": h1.hexdigest(), "md5": h5.hexdigest()}


def _steam_buildid(gd) -> str | None:
    """ビルドに使ったゲームの Steam のビルド番号（appmanifest の buildid）。ゲームの更新の目安として記録だけに入れる
    （mod/ のファイルには入れない。2026-10-17 機能 SF09）。Steam 以外のゲーム・偽のゲームデータは None。"""
    try:
        from .steam import game_build_info
        info = game_build_info(getattr(gd, "gd", None))
    except Exception:  # noqa: BLE001
        return None
    return info["buildid"] if info else None


def load_manifest(build_dir: str) -> dict:
    p = os.path.join(build_dir, MANIFEST)
    if not os.path.isfile(p):
        raise ModBuilderError(tr(f"ビルドの記録がありません: {p}", f"The build record is missing: {p}"))
    # 壊れた・形の違う記録は、場所の分かる ModBuilderError にする（verify・導入・MOD の書き出しが生の JSONDecodeError・
    # AttributeError のトレースバックになった。2026-10-27 R08A-03）
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ModBuilderError(tr(f"ビルドの記録を読めません（壊れています）: {p}（{type(e).__name__}）",
                                 f"Cannot read the build record (it is corrupted): {p} ({type(e).__name__})")) from None
    if not isinstance(data, dict) or not isinstance(data.get("files", {}), dict):
        # 文は verify の形の検査（cli._check_manifest_shape）と同じ。files が辞書でないと導入が AttributeError になった
        # （2026-10-27 R08B-03）
        raise ModBuilderError(tr(f"ビルドの記録の形が不正です（壊れています）: {p}",
                                 f"The build record has an invalid structure (it is corrupted): {p}"))
    return data


def _write(path: str, data: bytes | str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)


def _new_build_dir(out_dir: str | None) -> tuple[str, str]:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if out_dir:
        out_dir = os.path.abspath(out_dir)
        if os.path.isdir(out_dir) and os.listdir(out_dir):
            if not os.path.isfile(os.path.join(out_dir, MANIFEST)):
                raise ModBuilderError(tr(f"出力先が空ではなく、ModBuilder のビルドでもありません: {out_dir}",
                                         f"The output folder is not empty and is not a ModBuilder build: {out_dir}"))
            shutil.rmtree(out_dir)        # 以前のビルドの出力だけを作り直す
        return out_dir, stamp
    base = paths.builds_dir()
    d, n = os.path.join(base, stamp), 2
    while os.path.exists(d):
        d, n = os.path.join(base, f"{stamp}-{n}"), n + 1
    return d, os.path.basename(d)


def build(library: Library, gd, out_dir: str | None = None, languages=("EN", "JA"), custom_game: str | None = None,
          use_archivetool_check: bool = True, log=print, update_library: bool = True, archivetool=None,
          include_dev: bool = False) -> dict:
    """ライブラリの装備から統合ビルドを作る。成功すれば manifest を返す。問題があれば ValidationFailed。

    languages: 表示名を訳す言語。"JA" を含めば Text_JA.arc に日本語名を入れ、含まなければ英語名にする。Text_XX.arc は
        指定に依らず常に v1.3.1.1 の 13 言語分を作る（2026-10-17 利用者の判断 C124）。manifest の ``languages`` は作った
        Text_XX.arc の言語、``localized`` は日本語名を入れた言語。
    include_dev: 開発用の装備（``dev_only``。試作の装備）も入れる（開発用のビルド）。既定の False は製品版のビルドで、
        開発用の装備を除く（D-018）。除いた装備は manifest の ``excluded`` に記録し、状態は変えない。
    archivetool: .arc の作成・検査に使うもの（既定は ``<GD>/ArchiveTool.exe``。テストでは偽物を渡す）。"""
    languages = tuple(dict.fromkeys(l.upper() for l in (("EN", "JA") if languages is None else languages)))
    bad_langs = [l for l in languages if l not in LANGUAGES]
    if bad_langs:
        raise ModBuilderError(tr(f"未対応の言語: {', '.join(bad_langs)}（{', '.join(LANGUAGES)}）",
                                 f"Unsupported language: {', '.join(bad_langs)} (supported: {', '.join(LANGUAGES)})"))
    if custom_game is not None and custom_game.lower() in RESERVED_CUSTOM_NAMES:
        raise ModBuilderError(tr(f"Custom Game の名前 {custom_game!r} は公式のフォルダー（mods/survivalmode、Crucible）"
                                 "の名前なので使えません",
                                 f"The Custom Game name {custom_game!r} cannot be used because it is the name of an "
                                 "official folder (mods/survivalmode, Crucible)"))
    if custom_game is not None and custom_game.lower() in WINDOWS_RESERVED_NAMES:
        raise ModBuilderError(tr(f"Custom Game の名前 {custom_game!r} は Windows の予約された名前なので使えません",
                                 f"The Custom Game name {custom_game!r} cannot be used because it is a name reserved "
                                 "by Windows"))
    if custom_game is not None and not CUSTOM_NAME_RE.match(custom_game):
        raise ModBuilderError(tr(f"Custom Game の名前は小文字の英字で始まる英数字と _（2〜32 文字）: {custom_game!r}",
                                 "The Custom Game name must start with a lowercase letter and use only lowercase letters, "
                                 f"digits and _ (2 to 32 characters): {custom_game!r}"))
    if update_library:
        # 前回のアセットの入れ替えの途中の異常終了の残りを、記録と一致する側に戻す（戻さないと、その装備の set_assets が
        # 呼ばれるまで MOD の作成全体が止まった。2026-10-27 X6X-04）。アプリのビルドは装備の変更と同じ mod_lock の中で
        # 動くので、別の入れ替えの途中を残りと誤認しない。失敗してもビルドは続ける（下の verify_assets が問題にする）
        try:
            recovered = library.recover_interrupted_assets()
        except (LibraryError, OSError):
            recovered = {}
        if recovered:
            names = ", ".join(i + (tr("（戻した）", " (restored)") if s == "recovered" else tr("（戻せない）", " (not restored)"))
                              for i, s in sorted(recovered.items()))
            log(tr(f"前回の途中で止まったアセットの差し替えの残りを確かめました: {names}",
                   f"Checked the leftovers of an interrupted asset replacement: {names}"))
    all_equipment = library.list()
    equipment = [e for e in all_equipment if include_dev or not e.dev_only]
    excluded = [e for e in all_equipment if e not in equipment]
    if not equipment:
        if excluded:
            raise ModBuilderError(tr("ビルドに入れる装備がありません（開発用の装備だけのライブラリです。開発用のビルドは "
                                     "--dev / include_dev=True）",
                                     "There is no equipment to put into the build (the Library has only development "
                                     "equipment; for a development build use --dev / include_dev=True)"))
        raise ModBuilderError(tr("ビルドに入れる装備がありません", "There is no equipment to put into the build"))
    kept = {e.id for e in equipment}
    for e in equipment:
        if e.set_owner not in kept and e.set_owner in {x.id for x in excluded}:
            raise ModBuilderError(tr(f"装備 {e.id} は開発用の装備 {e.set_owner} の消耗品を共有しているため、製品版のビルドに入れられません",
                                     f"Equipment {e.id} shares the consumable of development equipment {e.set_owner}, "
                                     "so it cannot be put into a release build"))
    if archivetool is None:
        try:
            archivetool = ArchiveTool(gd.gd)
        except FileNotFoundError as e:
            # ゲームのフォルダーはあるが付属のツールが無い（点検 C82。task_build で GAME_NOT_FOUND の reason
            # "game_tool_missing"。FG05-R2）
            raise GameToolMissing(f"{e}{tool_missing_hint()}", path=os.path.join(gd.gd, "ArchiveTool.exe")) from None
    tool = archivetool
    bdir, build_id = _new_build_dir(out_dir)
    staging = os.path.join(bdir, "staging")
    started = now_iso()
    manifest = {
        "format": "gdskin-build", "version": 1, "build_id": build_id, "created_at": started, "status": "running",
        "tool": {"name": "modbuilder", "version": __version__},
        "library": {"root": library.root, "equipment": [e.summary() for e in equipment]},
        "profile": "dev" if include_dev else "release",
        "excluded": [e.summary() for e in excluded],
        "game": gd.fingerprint(), "game_buildid": _steam_buildid(gd),
        "languages": list(LANGUAGES), "localized": [l for l in languages if l in LOCALIZED_LANGUAGES],
        "custom_game": custom_game, "files": {}, "install": {}, "records": [], "validation": {},
    }
    # バックエンド（gdskin_backend.worker_tasks）は手順の文の先頭（[ビルド・[1]〜[4]・[完了]。英語は [Build・[Done]）で
    # 進み具合を、「警告」（英語は Warning）で警告の行を見分ける
    profile = tr("開発用", "development") if include_dev else tr("製品版", "release")
    skipped = ""
    if excluded:
        names = ", ".join(e.name_en for e in excluded)
        skipped = tr(f"（開発用の装備 {len(excluded)} 件を除外: {names}）",
                     f" (excluded {len(excluded)} development equipment: {names})")
    log(tr(f"[ビルド {build_id}] {profile}、装備 {len(equipment)} 件 → {bdir}{skipped}",
           f"[Build {build_id}] {profile}, {len(equipment)} equipment → {bdir}{skipped}"))
    plan_owners: dict[str, list[str]] = {}
    asset_owner: dict[str, str] = {}

    def record_failure(msg: str, problems: list[str], keys: set | None = None, rep: Report | None = None,
                       ids: set | None = None) -> None:
        manifest["status"] = "failed"
        manifest["error"] = {"message": msg, "problems": problems}
        if rep is not None:
            manifest["validation"] = rep.to_dict()
        _write(os.path.join(bdir, MANIFEST), json.dumps(manifest, ensure_ascii=False, indent=1))
        if update_library:
            # ids: 原因の装備の ID（手順 1・3 の問題）。keys: 原因のレコード・アセットのパス（検証・往復検証の問題）。
            # どちらからも分からなければ全装備
            failed = set(ids or ()) | _equipment_for_keys(keys or set(), plan_owners, asset_owner)
            library.mark_build_failed(sorted(failed) if failed else [e.id for e in equipment], msg + ": " + "; ".join(problems[:5]))

    def fail(msg: str, problems: list[str], keys: set | None = None, rep: Report | None = None, ids: set | None = None):
        record_failure(msg, problems, keys, rep, ids)
        raise ValidationFailed(msg, problems)

    try:
        return _build_steps(library, gd, equipment, languages, custom_game, use_archivetool_check, log, update_library,
                            tool, bdir, build_id, staging, manifest, plan_owners, asset_owner, fail)
    except ValidationFailed:
        raise                       # fail() で記録済み
    except Exception as e:  # noqa: BLE001
        # 検証以外の理由で止まった（ArchiveTool の失敗・打ち切り、ファイルの読み書き、ゲームデータの読み取りなど）。
        # 作りかけのビルドの記録と装備の状態を残し、BuildStopped（ModBuilderError。task_build で MOD_BUILD_FAILED の
        # reason "stopped"）にする（点検 C83・FG05-R1）。元が ModBuilderError（GameDataError を含む。ゲームのフォルダーは
        # 見つかっていてビルドの途中なので GAME_NOT_FOUND にはしない）なら文はそのまま使う
        text = f"{type(e).__name__}: {e}" if not isinstance(e, ModBuilderError) else str(e)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        msg = tr("MOD の作成が途中で止まりました", "The MOD build stopped partway")
        try:
            # equipment_ids: 原因の装備の分かる失敗（レコードとタグの生成の装備ごとの問題。R06A-01・R06B-04）
            record_failure(msg, [lines[0] if lines else type(e).__name__] + lines[1:][-20:],
                           ids=set(getattr(e, "equipment_ids", None) or ()))
        except Exception:  # noqa: BLE001  記録に失敗しても元の例外を伝える
            pass
        if isinstance(e, BuildStopped):
            raise
        if isinstance(e, ModBuilderError):
            raise BuildStopped(str(e)) from e
        cause = lines[0] if lines else type(e).__name__
        raise BuildStopped(tr(f"{msg}（{cause}）", f"{msg} ({cause})")) from e


def _build_steps(library, gd, equipment, languages, custom_game, use_archivetool_check, log, update_library, tool,
                 bdir, build_id, staging, manifest, plan_owners, asset_owner, fail) -> dict:
    # --- 1. ライブラリのアセットの整合 ----------------------------------------------------
    problems = []
    bad_ids = set()             # 問題のあった装備だけを build_failed にする（全装備・配置できた装備にしない。R06A-03）
    for eq in equipment:
        p = library.verify_assets(eq)
        if p:
            problems += p
            bad_ids.add(eq.id)
    if problems:
        fail(tr("装備ライブラリのアセットが記録と一致しません", "The equipment assets in the Library do not match the records"),
             problems, ids=bad_ids)
    # 作成日時は決定的なビルドの時刻（source_filetime）になるので、ありえない値（受け取った装備・手で書き換えた記録の
    # 西暦 3100 年など）の装備は、その装備だけを build_failed にして止める（全装備が原因の分からない OSError で作成失敗に
    # なった。2026-10-27 R06B-01）
    for eq in equipment:
        try:
            t = _dt.datetime.fromisoformat(eq.created_at)
        except (TypeError, ValueError):
            continue                # 読めない値は source_filetime も使わない（今までどおり）
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        if not CREATED_AT_MIN <= t < CREATED_AT_MAX:
            problems.append(tr(f"{eq.id}: 作成日時が範囲外です（{CREATED_AT_MIN.year}〜{CREATED_AT_MAX.year - 1} 年）: "
                               f"{eq.created_at}",
                               f"{eq.id}: the creation date is out of range ({CREATED_AT_MIN.year} to "
                               f"{CREATED_AT_MAX.year - 1}): {eq.created_at}"))
            bad_ids.add(eq.id)
    if problems:
        fail(tr("装備の作成日時が不正です", "The creation date of some equipment is invalid"), problems, ids=bad_ids)

    # --- 2. レコードとタグ ---------------------------------------------------------------------
    log(tr("[1] レコードとタグを生成", "[1] Creating records and tags"))
    try:
        plan = plan_records(gd, equipment, languages)
    except GameDataError:
        raise                       # 雛形の公式レコードが無いなど（装備の問題ではない）
    except ModBuilderError as e:
        # 装備ごとの問題（メッシュが無い・消耗品の持ち主の不整合など）なら、その装備だけを build_failed にする（全装備を
        # build_failed にして、導入済み・作成済みの表示まで消えた。2026-10-27 R06A-01・R06B-04）
        # 雛形の公式レコードが無い（records._official は GameDataError でなく ModBuilderError）ときは全体の問題なので、
        # 装備を推定しない（メッシュの無い装備などが同時にあると、その装備だけの失敗として記録された）
        if not getattr(e, "equipment_ids", None) and not _templates_missing(gd):
            e.equipment_ids = _plan_problem_ids(equipment)
        raise
    plan_owners.update(plan.owners)
    for rel, fields in sorted(plan.records.items()):
        _write(os.path.join(staging, "database", rel.replace("/", os.sep)), dbr_text(fields))
    for lang, tags in plan.tags.items():
        _write(os.path.join(staging, "resources", "Text_" + lang, TAGS_FILE), tags_file_text(tags, lang))
    # --- 3. アセット ---------------------------------------------------------------------------------
    for eq in equipment:
        for f in eq.data["assets"]["files"]:
            gp = norm_path(f["game_path"])
            if not gp.startswith("items/"):
                problems.append(tr(f"{eq.id}: アセットのゲーム内パスは items/ で始まる必要があります: {gp}",
                                   f"{eq.id}: the in-game path of an asset must start with items/: {gp}"))
                bad_ids.add(eq.id)
                continue
            # '..'・'.'・空の要素や ':' があると、作業フォルダー（staging\resources\Items）の外に書きうる（2026-10-17 監査 C13）
            if ":" in gp or any(part in ("", ".", "..") for part in gp.split("/")):
                problems.append(tr(f"{eq.id}: アセットのゲーム内パスが不正です（'..'・':' などを含む）: {gp}",
                                   f"{eq.id}: invalid in-game path of an asset (it contains '..', ':' or similar): {gp}"))
                bad_ids.add(eq.id)
                continue
            # MOD のアイコンの置き場所（items/aiskin/icons/）と自前の透明の見た目の置き場所（items/aiskin/invisible/。D-079）は
            # このアプリが使う（装備のファイルで上書きさせない。2026-11-11）
            reserved = next((p for p in (ICON_PREFIX, INVISIBLE_LOOK_PREFIX) if gp.startswith(p)), None)
            if reserved:
                problems.append(tr(f"{eq.id}: アセットのゲーム内パスが、このアプリが使う置き場所（{reserved}）にあります: {gp}",
                                   f"{eq.id}: the in-game path of an asset is in a place this app uses ({reserved}): {gp}"))
                bad_ids.add(eq.id)
                continue
            if gp in asset_owner:
                problems.append(tr(f"アセットのパス {gp} が装備 {asset_owner[gp]} と {eq.id} で重なっています",
                                   f"Asset path {gp} is used by both equipment {asset_owner[gp]} and {eq.id}"))
                bad_ids.update((asset_owner[gp], eq.id))
                continue
            asset_owner[gp] = eq.id
            dst = os.path.join(staging, "resources", "Items", gp.split("/", 1)[1].replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(eq.asset_path(f["file"]), dst)
    if problems:
        fail(tr("アセットの配置に問題があります", "There are problems placing the assets"), problems, ids=bad_ids)
    # MOD のアイテムのアイコン（消耗品・頭装備・透明の部位。レコードが参照するものだけ。2026-11-11）
    icon_paths = write_icons(os.path.join(staging, "resources", "Items"),
                             icons_needed(bool(plan.equipment), bool(plan.set_records)))
    # 自前の透明の肩・メダルが使う、公式の透明と同じ見た目のメッシュと画像（透明の部位のレコードは常に作る。D-079）
    look_paths = write_look(os.path.join(staging, "resources", "Items"), gd)
    ja_names = tr("、日本語名あり", ", with Japanese names") if manifest["localized"] else ""
    log(tr(f"    レコード {len(plan.records)}、タグ {len(plan.tags['EN'])}（Text_XX {len(plan.tags)} 言語{ja_names}）、"
           f"アセット {len(asset_owner)}、アイコン {len(icon_paths)}、透明の見た目 {len(look_paths)}、"
           f"消耗品 {len(plan.set_records)}",
           f"    Records {len(plan.records)}, tags {len(plan.tags['EN'])} "
           f"(Text_XX: {len(plan.tags)} languages{ja_names}), assets {len(asset_owner)}, icons {len(icon_paths)}, "
           f"invisible look files {len(look_paths)}, consumables {len(plan.set_records)}"))

    # --- 4. 検証 -------------------------------------------------------------------------------------
    log(tr("[2] 検証（参照先・タグ・差分コピー・.msh・.tex）", "[2] Validation (references, tags, modified copies, .msh, .tex)"))
    rep = validate_staging(staging, gd, LANGUAGES)
    for w in rep.warnings:
        log(tr(f"    警告: {w}", f"    Warning: {w}"))
    if rep.errors:
        fail(tr(f"検証で {len(rep.errors)} 件の問題が見つかったため、ビルドを止めました",
                f"The build was stopped because validation found {len(rep.errors)} problem(s)"),
             rep.errors, rep.error_keys, rep)
    log(tr(f"    エラー 0、警告 {len(rep.warnings)}（{rep.checks}）",
           f"    Errors: 0, Warnings: {len(rep.warnings)} ({rep.checks})"))

    # --- 5. .arz / .arc ------------------------------------------------------------------------------
    log(tr("[3] database.arz と .arc を作成", "[3] Creating database.arz and the .arc files"))
    tmp_mod = os.path.join(bdir, "mod.tmp")
    if os.path.isdir(tmp_mod):
        shutil.rmtree(tmp_mod)
    db_root = os.path.join(staging, "database")
    records = {}
    for dirpath, _d, files in os.walk(db_root):
        for f in files:
            full = os.path.join(dirpath, f)
            records[norm_path(os.path.relpath(full, db_root))] = read_dbr(full)
    ft = source_filetime(gd, equipment, plan.sources.values())      # 決定的なビルド（冒頭の説明）
    # fromtimestamp は使わない（Windows では西暦 3001 年以降で OSError になり、ビルドが毎回止まった。R06B-01）
    try:
        utc = (_dt.datetime(1601, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(microseconds=ft // 10)).isoformat()
    except OverflowError:
        utc = None
    manifest["source_time"] = {"filetime": ft, "utc": utc}
    info = compile_records(records, gd.templates, os.path.join(tmp_mod, "database.arz"), ft)
    for w in info["warnings"]:
        log(tr(f"    警告: {w}", f"    Warning: {w}"))
    outputs = {"database.arz": os.path.join(tmp_mod, "database.arz")}
    for arcname in ["Text_" + l for l in LANGUAGES] + (["Items"] if asset_owner or icon_paths or look_paths else []):
        out = os.path.join(tmp_mod, arcname + ".arc")
        src_dir = os.path.join(staging, "resources", arcname)
        _set_tree_mtime(src_dir, ft)
        tool.build(src_dir, out)
        outputs[arcname + ".arc"] = out

    # --- 6. 往復検証 ---------------------------------------------------------------------------------
    if use_archivetool_check:
        log(tr("[4] 往復検証（自前の読み取り・ArchiveTool・差分コピーの値）",
               "[4] Round-trip verification (built-in reader, ArchiveTool, values in modified copies)"))
    else:
        log(tr("[4] 往復検証（自前の読み取り・差分コピーの値）",
               "[4] Round-trip verification (built-in reader, values in modified copies)"))
    vrep = verify_outputs(staging, outputs, tool if use_archivetool_check else None)
    crep = verify_copies(outputs["database.arz"], gd, plan.sources, staging)
    rep.checks.update({"roundtrip_" + k: v for k, v in vrep.checks.items()})
    rep.checks.update({"roundtrip_" + k: v for k, v in crep.checks.items()})
    for w in vrep.warnings + crep.warnings:
        log(tr(f"    警告: {w}", f"    Warning: {w}"))
        rep.warnings.append(w)
    if vrep.errors or crep.errors:
        rep.errors += vrep.errors + crep.errors
        fail(tr("生成したファイルの往復検証に失敗しました", "Round-trip verification of the created files failed"),
             vrep.errors + crep.errors, vrep.error_keys | crep.error_keys, rep)
    mod_dir = os.path.join(bdir, "mod")
    os.replace(tmp_mod, mod_dir)
    files = {name: dict(file_hashes(os.path.join(mod_dir, name)), path="mod/" + name) for name in sorted(outputs)}
    manifest["files"]["basemods"] = files
    manifest["install"]["basemods"] = {name: "mod/" + name for name in sorted(outputs)}

    if custom_game:
        cdir = os.path.join(bdir, "custom", custom_game)
        cfiles = {}
        for name in sorted(outputs):
            if name == "database.arz":
                rel = f"database/{custom_game}.arz"
            else:
                rel = f"resources/{name}"
            dst = os.path.join(cdir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(mod_dir, name), dst)
            cfiles[rel] = dict(file_hashes(dst), path=f"custom/{custom_game}/{rel}")
        manifest["files"]["custom"] = cfiles
        manifest["install"]["custom"] = {rel: f"custom/{custom_game}/{rel}" for rel in cfiles}

    manifest["status"] = "ok"
    manifest["finished_at"] = now_iso()
    manifest["records"] = [{"name": rel, "template": plan.sources.get(rel), "equipment": plan.owners.get(rel, [])}
                           for rel in sorted(plan.records)]
    manifest["set_records"] = plan.set_records
    manifest["assets"] = {gp: asset_owner[gp] for gp in sorted(asset_owner)}
    manifest["icons"] = sorted(icon_paths)          # MOD のアイテムのアイコン（2026-11-11。装備には属さない）
    manifest["invisible_look"] = sorted(look_paths)  # 自前の透明の肩・メダルの見た目のファイル（2026-11-11 D-079）
    manifest["validation"] = rep.to_dict()
    _write(os.path.join(bdir, MANIFEST), json.dumps(manifest, ensure_ascii=False, indent=1))
    _write(os.path.join(bdir, "validation_report.txt"), _report_text(manifest, rep))
    if update_library:
        try:
            library.mark_built([e.id for e in equipment], build_id)
        except Exception as e:  # noqa: BLE001
            # mod/ と記録は完成しているので、装備の状態の更新の失敗でビルドを失敗にしない（R06B-05）。警告として記録に残す
            w = tr(f"装備の状態（MOD 作成済み）の更新に失敗しました: {type(e).__name__}: {e}",
                   f"Failed to update the equipment status (MOD built): {type(e).__name__}: {e}")
            log(tr(f"    警告: {w}", f"    Warning: {w}"))
            rep.warnings.append(w)
            manifest["validation"] = rep.to_dict()
            _write(os.path.join(bdir, MANIFEST), json.dumps(manifest, ensure_ascii=False, indent=1))
    sizes = ", ".join(f"{n} {v['size']:,} B" for n, v in files.items())
    log(tr(f"[完了] {sizes}", f"[Done] {sizes}"))
    manifest["build_dir"] = bdir
    return manifest


def _templates_missing(gd) -> bool:
    """plan_records が使う雛形の公式レコード（records._official）のどれかが無いか（確かめられなければ True）。"""
    try:
        # 透明の部位の雛形は公式の透明の肩と、2026-11-11（D-078）から公式の透明のメダル
        return any(gd.official_record(rel) is None
                   for rel in (TEMPLATE_HEAD, TEMPLATE_SET, *sorted(set(INVISIBLE_SOURCE.values())), *MERCHANT_RECORDS))
    except Exception:  # noqa: BLE001  ゲームデータの読み取りの失敗も全体の問題として扱う
        return True


def _plan_problem_ids(equipment) -> set[str]:
    """plan_records が装備ごとの問題で止めたときの、原因の装備ID（plan_records と同じ条件: メッシュが無い、消耗品の
    持ち主が無い・持ち主自身が別の消耗品に属する・持ち主に消耗品のレコード名が無い、レコードのパスの重なり、表示名・
    表示文の改行の文字）。見つからなければ空（呼び出し側は全装備にする）。"""
    by_id = {e.id: e for e in equipment}
    bad, seen = set(), {}
    for e in equipment:
        a = e.data["assets"]
        if not a.get("male_mesh") or not a.get("female_mesh"):
            bad.add(e.id)
        owner = by_id.get(e.set_owner)
        if owner is None:
            bad.add(e.id)
        elif not owner.owns_set:
            bad.update((e.id, owner.id))
        if e.owns_set and not e.naming.get("set_record"):
            bad.add(e.id)
        for rel in (e.naming.get("head_record"), e.naming.get("set_record") if e.owns_set else None):
            if rel is None:
                continue
            if rel in seen:
                bad.update((seen[rel], e.id))
            seen.setdefault(rel, e.id)
        texts = [e.data["display_name"].get("en"), e.data["display_name"].get("ja")] + \
            [v for langs in (e.data.get("texts") or {}).values() if isinstance(langs, dict) for v in langs.values()]
        if any(isinstance(v, str) and "".join(v.splitlines()) != v for v in texts):
            bad.add(e.id)
    return bad


def _equipment_for_keys(keys: set, owners: dict[str, list[str]], asset_owner: dict[str, str]) -> set[str]:
    out = set()
    for k in keys:
        for i in owners.get(k, []):
            out.add(i)
        if k in asset_owner:
            out.add(asset_owner[k])
    return out


def _report_text(manifest: dict, rep: Report) -> str:
    lines = [tr(f"ModBuilder ビルド {manifest['build_id']}（{manifest['created_at']}）",
                f"ModBuilder build {manifest['build_id']} ({manifest['created_at']})"),
             tr(f"状態: {manifest['status']}", f"Status: {manifest['status']}"), ""]
    lines.append(tr("[ファイル]", "[Files]"))
    for kind, files in manifest["files"].items():
        for n, v in files.items():
            lines.append(f"  {kind}: {n}  {v['size']:,} B  sha256 {v['sha256']}  md5 {v['md5']}")
    lines += ["", tr("[検査数] ", "[Checks] ") + json.dumps(rep.checks, ensure_ascii=False), "",
              tr(f"[エラー] {len(rep.errors)}", f"[Errors] {len(rep.errors)}")]
    lines += ["  " + e for e in rep.errors]
    lines += ["", tr(f"[警告] {len(rep.warnings)}", f"[Warnings] {len(rep.warnings)}")] + ["  " + w for w in rep.warnings]
    lines += ["", tr("[詳細]", "[Details]")] + ["  " + i for i in rep.info]
    return "\n".join(lines) + "\n"
