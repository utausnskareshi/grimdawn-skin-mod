"""装備ライブラリの操作（計画書 10 章: 作成・再編集・削除・再生成。docs/app_architecture.md 5.3〜5.4 節）。

形式は modbuilder の装備ライブラリ（``docs/modbuilder.md`` 3 章）をそのまま使う。アプリはプレビュー用のファイルを
装備の ``source\\`` に足す。書き換える操作は ``svc.mod_lock`` を取る（ビルド・導入と同時に行わない）。
"""
from __future__ import annotations

import errno
import os
import shutil

from . import logs
from .errors import BackendError
from .i18n import tr

PREVIEW_FILES = {"generated_glb": "generated.glb", "preview_m_glb": "preview_m.glb", "preview_f_glb": "preview_f.glb",
                 "candidates_png": "candidates.png", "preview_png": "preview.png"}


def _lib(svc, create: bool = True):
    from modbuilder.library import Library
    return Library.open(svc.library_dir(), create=create)


def _lock(svc, timeout: float = 2.0):
    if not svc.mod_lock.acquire(timeout=timeout):
        raise BackendError("BUSY", tr("MOD の作成・導入の実行中は装備を変更できません。終わってからもう一度操作してください",
                                      "Equipment cannot be changed while a MOD build or installation is running. "
                                      "Try again after it finishes"))
    return svc.mod_lock


JOB_LOCK_WAIT = 900.0       # ジョブが mod_lock を待つ上限（秒）。MOD の作成の ArchiveTool の打ち切り（300 秒）より長く


def _lock_for_job(svc, ctx, progress: float, wait_stage: str, stage: str, limit: float | None = None):
    """ジョブの中で mod_lock を取る。MOD の列のジョブ（作成・導入・書き出し・取り込み）の実行中は、取消を確かめながら
    終わるまで待つ（以前は 30 秒で BUSY になり、終わった変換の結果を捨てた。レビュー R12A-04）。待つ間は進捗に
    wait_stage を出し、取れたら stage に戻す。limit（省くと JOB_LOCK_WAIT）秒を過ぎたら BUSY。"""
    import time
    limit = JOB_LOCK_WAIT if limit is None else limit
    if svc.mod_lock.acquire(timeout=0.5):
        return svc.mod_lock
    ctx.progress(progress, wait_stage)
    t0 = time.monotonic()
    while True:
        ctx.check_cancel()
        if svc.mod_lock.acquire(timeout=0.5):
            ctx.progress(progress, stage)
            return svc.mod_lock
        if time.monotonic() - t0 > limit:
            raise BackendError("BUSY", tr("MOD の作成・導入の実行中は装備を変更できません。終わってからもう一度操作してください",
                                          "Equipment cannot be changed while a MOD build or installation is running. "
                                          "Try again after it finishes"))


def _wrap(fn):
    from modbuilder.errors import LibraryError, ModBuilderError

    def inner(*a, **kw):
        try:
            return fn(*a, **kw)
        except LibraryError as e:
            msg = str(e)
            # modbuilder の文は表示言語の文なので、その言語の語で分ける（英語の文は大文字小文字を問わず "not found"）
            code = "NOT_FOUND" if tr("見つかりません", "not found") in msg.lower() else "INVALID_INPUT"
            raise BackendError(code, msg)
        except ModBuilderError as e:
            raise BackendError("INVALID_INPUT", str(e))
    inner.__name__ = fn.__name__
    return inner


ITEM_TEXT_ROLES = ("set", "set_desc")      # 消耗品の名前・説明文（texts の役割。head は表示名の変更で扱う）


def _item_texts(eq, members=None) -> dict:
    """消耗品の表示文の上書き（texts の set・set_desc。無ければ None）と、空欄のときに使う既定の文（2026-10-17 機能 SF11）。
    既定の文は modbuilder.records.default_texts と同じもの（上書きを外して計算）。members は消耗品を共有する装備
    （省くと自分だけ。アプリで作った装備は自分の消耗品だけを持つ）。"""
    texts = eq.data.get("texts") or {}
    cur = {r: {lang: (texts.get(r) or {}).get(lang) for lang in ("en", "ja")} for r in ITEM_TEXT_ROLES}
    defaults = None
    try:
        from modbuilder.library import Equipment
        from modbuilder.records import default_texts
        plain = lambda e: Equipment({**e.data, "texts": {}}, e.dir)      # noqa: E731
        dt = default_texts(plain(eq), [plain(m) for m in (members or [eq])],
                           bool((eq.data.get("illusion_set") or {}).get("include_invisible", True)))
        defaults = {r: dict(dt[r]) for r in ITEM_TEXT_ROLES}
    except Exception:   # noqa: BLE001  壊れた記録でも一覧は出す
        defaults = None
    return {"item_texts": cur, "item_text_defaults": defaults}


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _input_image_path(eq, sp) -> str | None:
    """記録の入力画像（stored_path）のファイルの絶対パス。装備の source\\ の直下の input で始まる名前のファイル
    （storage._stored_file と同じ規則）だけで、それ以外（絶対パス・「..」・UNC パス・文字列でない値）はファイルを
    確かめずに None（受け取った記録・書き換えた記録で、装備のフォルダーの外を入力画像にしない。レビュー R12A-03）。"""
    from .storage import _stored_file
    p = _stored_file(eq, sp) if isinstance(sp, str) and sp else None
    return p if p and os.path.isfile(p) else None


def equipment_view(eq, members=None) -> dict:
    # 記録の項目の型が違っても（取り込んだ記録・手で書き換えた記録）一覧全体を止めない（レビュー R12B-01）
    d = eq.data
    gen = _dict(d.get("generation"))
    files = {}
    for key, name in PREVIEW_FILES.items():
        p = os.path.join(eq.dir, "source", name)
        if os.path.isfile(p):
            files[key] = p
    img = _input_image_path(eq, _dict(gen.get("input_image")).get("stored_path"))
    if img:
        files["input_image"] = img
    input_images = []
    for r in gen.get("input_images") if isinstance(gen.get("input_images"), list) else []:
        r = _dict(r)
        input_images.append({"label": r.get("label"), "path": _input_image_path(eq, r.get("stored_path")),
                             "force_bg_removal": bool(r.get("force_bg_removal"))})     # 再生成で画像の欄に戻す（SF19）
    conv = _dict(d.get("conversion"))
    summary = _dict(conv.get("summary"))
    return {
        **eq.summary(),
        "updated_at": d.get("updated_at"),
        "generation": {k: gen.get(k) for k in ("method", "input_text", "model_id", "seed", "settings", "generated_at",
                                               "provider", "image_count", "multi_mode", "variant_of")},
        "input_images": input_images,
        "has_input_image": "input_image" in files,
        "conversion_warnings": conv.get("warnings") if isinstance(conv.get("warnings"), list) else [],
        "yaw_deg": conv.get("yaw_deg") or 0,
        "quality_flags": _dict(summary.get("quality_flags")),
        "vertices": {g: _dict(summary.get(g)).get("vertices") for g in ("m", "f")},
        "mod_detail": {k: _dict(d.get("mod")).get(k) for k in ("status", "last_build_id", "installed_build_id", "error")},
        "errors": d.get("errors")[:5] if isinstance(d.get("errors"), list) else [],
        "files": files,
        "dir": eq.dir,
        "owns_set": eq.owns_set,
        "set_owner": _dict(d.get("illusion_set")).get("owner"),
        **_item_texts(eq, members),
    }


def _set_members(lib, eq):
    """消耗品を共有する装備（持ち主なら自分を含む、作成順。持ち主でなければ None）。既定の説明文に名前が並ぶので、
    GET /library・GET /library/<id>・各操作の応答で同じ既定の文になるように使う（U-C-R5）。"""
    if not eq.owns_set:
        return None
    return [e for e in lib.list() if e.set_owner == eq.id] or None


@_wrap
def list_equipment(svc) -> list[dict]:
    lib = _lib(svc)
    all_eq = lib.list()
    members = {}
    for e in all_eq:                                    # 消耗品ごとの装備（作成順。既定の説明文に名前が並ぶ）
        members.setdefault(e.set_owner, []).append(e)
    items = [equipment_view(e, members.get(e.id) if e.owns_set else None) for e in all_eq]
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items


@_wrap
def get_equipment(svc, eq_id: str) -> dict:
    lib = _lib(svc)
    if eq_id not in lib.ids():
        raise BackendError("NOT_FOUND", tr(f"装備がありません: {eq_id}", f"Equipment not found: {eq_id}"))
    eq = lib.load(eq_id)
    return {**equipment_view(eq, _set_members(lib, eq)), "data": eq.data}


def _name_warnings(name_en: str | None) -> list[str]:
    """英語名の注意（保存は止めない。英語の設定のゲームのフォントは ASCII だけ。2026-10-17 点検 C75）。"""
    from modbuilder.library import display_name_warnings
    return display_name_warnings(name_en, "en") if name_en else []


@_wrap
def rename(svc, eq_id: str, name_en: str | None, name_ja: str | None) -> dict:
    """表示名を変える。応答の ``warnings`` は保存を止めない注意（英語名に ASCII の外の文字がある、など）。注意は英語名を
    変えたときだけ返す（画面は日本語名だけを直すときも英語名を送るので、前と同じ英語名では毎回出さない。U-C-R4）。"""
    lk = _lock(svc)
    try:
        lib = _lib(svc)
        if eq_id not in lib.ids():
            raise BackendError("NOT_FOUND", tr(f"装備がありません: {eq_id}", f"Equipment not found: {eq_id}"))
        old_en = lib.load(eq_id).name_en
        eq = lib.rename(eq_id, name_en=name_en, name_ja=name_ja)
        logs.info(tr(f"装備の表示名を変更: {eq_id}", f"Changed the equipment name: {eq_id}"))
        warnings = _name_warnings(name_en) if name_en != old_en else []
        return {**equipment_view(eq, _set_members(lib, eq)), "warnings": warnings}
    finally:
        lk.release()


@_wrap
def remove(svc, eq_id: str) -> dict:
    lk = _lock(svc)
    try:
        lib = _lib(svc)
        if eq_id not in lib.ids():
            raise BackendError("NOT_FOUND", tr(f"装備がありません: {eq_id}", f"Equipment not found: {eq_id}"))
        dest = lib.remove(eq_id)
        logs.info(tr(f"装備を装備一覧から外しました（trash へ移動）: {eq_id}",
                     f"Removed the equipment from the Library (moved to trash): {eq_id}"))
        return {"id": eq_id, "trash": dest}
    finally:
        lk.release()


# ---------------------------------------------------------------------------
# 削除した装備（trash）の一覧と復元（2026-10-17 機能 SF02）
# ---------------------------------------------------------------------------
@_wrap
def list_trash(svc) -> list[dict]:
    """trash の装備（外した日時の新しい順）。``restorable`` が false の項目は ``problems`` に戻せない理由
    （同じ内部IDの装備が一覧にある・レコードのパスやタグ名の重なり・消耗品の持ち主が無い・記録が読めない）。"""
    lib = _lib(svc)
    out = []
    for t in lib.trash_items():
        files = {"preview_png": t["preview_png"]} if t.get("preview_png") else {}
        out.append({**{k: v for k, v in t.items() if k != "preview_png"}, "files": files})
    return out


@_wrap
def restore_trash(svc, name: str) -> dict:
    """trash の装備（フォルダー名 ``name``）を、同じ内部ID・レコードのまま装備一覧に戻す。戻せない理由があれば
    INVALID_INPUT（reason ``restore_conflict``、params.problems）で何も変えない。MOD は「作成が必要」になる。"""
    from modbuilder.errors import LibraryError
    lk = _lock(svc)
    try:
        lib = _lib(svc)
        try:
            src = lib._trash_dir(str(name or ""))        # 一覧の名前と完全に一致するフォルダーだけ（別名は NOT_FOUND。U-C-R1）
        except LibraryError as e:
            raise BackendError("INVALID_INPUT", str(e), params={"reason": "trash_name"}) from None
        if src is None:
            raise BackendError("NOT_FOUND", tr(f"trash に装備が見つかりません: {name}", f"Equipment not found in trash: {name}"))
        eq0 = lib._load_trashed(src)
        problems = lib.trash_restore_problems(eq0)
        if problems:
            raise BackendError("INVALID_INPUT", tr(f"装備 {eq0.id} を元に戻せません: ", f"Cannot restore equipment {eq0.id}: ")
                               + "; ".join(problems),
                               params={"reason": "restore_conflict", "problems": problems, "equipment_id": eq0.id})
        eq = lib.restore_from_trash(name)
        logs.info(tr(f"装備を trash から装備一覧に戻しました: {eq.id}", f"Restored the equipment from trash to the Library: {eq.id}"))
        return {**equipment_view(eq, _set_members(lib, eq)), "restored_from": name}
    finally:
        lk.release()


# ---------------------------------------------------------------------------
# 消耗品の名前・説明文の編集（texts の set・set_desc。空欄なら既定の文。2026-10-17 機能 SF11）
# ---------------------------------------------------------------------------
@_wrap
def set_item_texts(svc, eq_id: str, texts) -> dict:
    """``texts`` = {"set"|"set_desc": {"en"|"ja": 文 | "" | None}}。空（空白だけを含む）・None はその欄の上書きを外す
    （既定の文に戻す）。指定しなかった欄と、今と同じ値の欄は変えない（MOD の状態も変えない）。全部の欄を先に検査し、
    1 つでも問題があれば何も変えない。応答の ``warnings`` は保存を止めない注意（英語の文に ASCII の外の文字）、
    ``changed`` は変えた欄 [\"set.en\", ...]。"""
    from modbuilder.library import DESC_MAX, NAME_MAX, validate_display_name
    if not isinstance(texts, dict) or not texts:
        raise BackendError("INVALID_INPUT", tr("texts（消耗品の名前・説明文）を指定してください",
                                               "Specify texts (the consumable's name and description)"),
                           params={"reason": "item_texts"})
    wanted, problems = {}, []
    for role, langs in texts.items():
        if role not in ITEM_TEXT_ROLES or not isinstance(langs, dict):
            raise BackendError("INVALID_INPUT", tr(f"texts の項目が不正です: {role!r}（{', '.join(ITEM_TEXT_ROLES)}）",
                                                   f"Invalid texts field: {role!r} ({', '.join(ITEM_TEXT_ROLES)})"),
                               params={"reason": "item_texts"})
        for lang, v in langs.items():
            if lang not in ("en", "ja") or not (v is None or isinstance(v, str)):
                raise BackendError("INVALID_INPUT", tr(f"texts.{role} の項目が不正です: {lang!r}",
                                                       f"Invalid texts.{role} field: {lang!r}"),
                                   params={"reason": "item_texts"})
            v = v if v is not None and v.strip() else None
            if v is not None:
                what = tr("消耗品の名前", "Consumable name") if role == "set" else tr("説明文", "Description")
                in_lang = tr("英語", "English") if lang == "en" else tr("日本語", "Japanese")
                label = tr(f"{what}（{in_lang}）", f"{what} ({in_lang})")
                # 表示名の検査の文（modbuilder の表示言語の文）を、この欄の文にする（「表示名」→「文」。英語の文は主語の無い
                # 文（"too long (…)" など）なので、ふつうは変わらない）
                problems += [f"{label}: {p.replace(tr('表示名', 'name'), tr('文', 'text'))}"
                             for p in validate_display_name(v, lang, DESC_MAX if role == "set_desc" else NAME_MAX)]
            wanted[(role, lang)] = v
    if problems:
        raise BackendError("INVALID_INPUT", tr("消耗品の名前・説明文: ", "Consumable name and description: ") + "; ".join(problems),
                           params={"reason": "item_texts", "problems": problems})
    lk = _lock(svc)
    try:
        lib = _lib(svc)
        if eq_id not in lib.ids():
            raise BackendError("NOT_FOUND", tr(f"装備がありません: {eq_id}", f"Equipment not found: {eq_id}"))
        eq = lib.load(eq_id)
        if not eq.owns_set:
            raise BackendError("INVALID_INPUT", tr(f"この装備の消耗品は装備 {eq.set_owner} にまとめられています。その装備で編集してください",
                                                   f"This equipment's consumable is grouped with equipment {eq.set_owner}. "
                                                   "Edit it from that equipment"),
                               params={"reason": "item_texts_not_owner", "owner": eq.set_owner})
        changed = []
        for (role, lang), v in wanted.items():
            if eq.text(role, lang) != v:
                eq = lib.set_texts(eq_id, role, lang, v)
                changed.append(f"{role}.{lang}")
        tx = eq.data.get("texts") or {}
        if any(isinstance(tx.get(r), dict) and not tx[r] for r in ITEM_TEXT_ROLES):
            eq.data["texts"] = {k: v for k, v in tx.items() if not (k in ITEM_TEXT_ROLES and not v)}   # 空の役割は残さない
            lib.save(eq, touch=False)
        if changed:
            logs.info(tr(f"消耗品の名前・説明文を変更: {eq_id}（{', '.join(changed)}）",
                         f"Changed the consumable's name and description: {eq_id} ({', '.join(changed)})"))
        warnings = []
        for role in ITEM_TEXT_ROLES:
            label = tr("消耗品の名前（英語）", "Consumable name (English)") if role == "set" else tr("説明文（英語）", "Description (English)")
            # 英語名の注意の文（modbuilder の表示言語の文）を、この欄の文にする（「日本語名の欄」→「日本語の欄」。英語の文は
            # 初めから "Japanese field" なので変わらない）
            warnings += [f"{label}: {w.replace(tr('日本語名の欄', 'Japanese name field'), tr('日本語の欄', 'Japanese field'))}"
                         for w in _name_warnings(wanted.get((role, "en")))]
        eq = lib.load(eq_id)
        return {**equipment_view(eq, _set_members(lib, eq)), "warnings": warnings, "changed": changed}
    finally:
        lk.release()


def _generation_info(draft) -> dict:
    from . import variant
    d = draft.data
    inp = d.get("input") or {}
    # 見た目の作り直し（SF10）の生成方法は text（文章・TRELLIS-text-xlarge・シード）。元の装備は variant_of に残す
    method = {"image": "image", "text": "text", "variant": "text"}.get(inp.get("kind"), "external")
    gen = {"method": method, "model_id": d.get("model_id"), "seed": d.get("seed"),
           "settings": {**(d.get("settings") or {}), "postprocess": "postprocess_alt" if d.get("provider") == "trellis" else None},
           "generated_at": d.get("created_at"), "provider": d.get("provider"),
           "input_text": inp.get("text") if method == "text" else None, "input_image": None}
    picked = ((d.get("generation") or {}).get("info") or {}).get("picked")
    if isinstance(picked, dict):
        # 利用者が選んだ候補・保存した生成結果からの GLB の作り直し（SF06・SF14。無いときは項目を作らない）
        gen["candidate_choice"] = dict(picked)
    vo = variant.record(d)
    if vo is not None:                  # 見た目の作り直し（SF10。使ったときだけ項目を作る）
        gen["variant_of"] = vo
    if method == "image":
        recs = inp.get("images") or ([inp["image"]] if isinstance(inp.get("image"), dict) else [])
        # 元のパス（利用者のフォルダー名を含む）は記録しない。ファイル名と SHA-256 だけ
        gen["input_images"] = [{"label": r.get("label"), "original_name": r.get("original_name"), "sha256": r.get("sha256"),
                                "size": r.get("size"), "has_alpha": r.get("has_alpha"), "stored_path": None,
                                # 既定から変えた背景の扱い（SF19 背景除去の強制・SF20 アプリ側の背景除去。使ったときだけ。U-B）
                                **{k: r[k] for k in ("force_bg_removal", "bg_removal") if r.get(k)}} for r in recs]
        gen["image_count"] = len(recs)
        im = recs[0] if recs else {}
        gen["input_image"] = {"original_path": None, "original_name": im.get("original_name"), "stored_path": None,
                              "sha256": im.get("sha256"), "size": im.get("size")}
        gen["multi_mode"] = (d.get("settings") or {}).get("multi_mode") if len(recs) > 1 else None
    return gen


def _discard_new_equipment(eq) -> None:
    """保存の途中で失敗した、作ったばかりの装備のフォルダーを消す（import_converted の失敗時と同じ）。"""
    shutil.rmtree(eq.dir, ignore_errors=True)
    if os.path.lexists(eq.dir):
        logs.warning(tr(f"保存に失敗した装備 {eq.id} のフォルダーを消し切れませんでした: {eq.dir}",
                        f"Could not fully delete the folder of the equipment {eq.id} that failed to save: {eq.dir}"))


@_wrap
def save_draft(svc, draft_id: str, name_en: str, name_ja: str | None = None, keep_image: bool | None = None,
               replace_equipment_id: str | None = None, as_new: bool = False) -> dict:
    """下書きを装備ライブラリに保存する。replace_equipment_id があれば、その装備の変換結果を差し替える（再生成）。
    replace_equipment_id を省くと、下書きに記録した差し替え先（再生成のジョブが記録する）を使う。as_new が真なら記録を
    使わず新しい装備として保存する（画面の「新規として作る」・装備一覧の「新規作成」の後や、起動時に復元した再生成の
    下書きを新規として保存するとき。以前は記録に戻り、元の装備が差し替えられて改名された。点検 R18A-01）。
    応答の ``warnings`` は保存を止めない注意（英語名に ASCII の外の文字がある、など。点検 C75）。"""
    from modbuilder.importers import import_converted, replace_converted
    from modbuilder.library import validate_display_name

    s = svc.settings.get()
    keep_image = s["keep_input_image"] if keep_image is None else bool(keep_image)
    draft = svc.drafts.get(draft_id)
    if draft.data.get("status") != "ready":
        raise BackendError("INVALID_INPUT", tr(f"この下書きはまだ保存できません（状態 {draft.data.get('status')}）",
                                               f"This draft cannot be saved yet (state {draft.data.get('status')})"))
    if as_new and replace_equipment_id:
        raise BackendError("INVALID_INPUT", tr("新しい装備としての保存と、差し替える装備は同時に指定できません",
                                               "Saving as new equipment and the equipment to replace cannot be specified "
                                               "together"))
    replace_equipment_id = None if as_new else (replace_equipment_id or draft.data.get("replace_equipment_id"))
    if not replace_equipment_id:
        problems = validate_display_name(name_en or "", "en") + (validate_display_name(name_ja, "ja") if name_ja else [])
    elif name_en:
        # 差し替えでも、アセットを差し替える前に改名の名前を確かめる（空の英語名は名前を変えない。以前は差し替えの後の
        # 改名が名前の検査で失敗し、アセットだけが置き換わった。2026-10-27 検証 R12A-03）
        problems = validate_display_name(name_en, "en") + (validate_display_name(name_ja, "ja") if name_ja else [])
    else:
        problems = []
    if problems:
        raise BackendError("INVALID_INPUT", tr("装備名: ", "Equipment name: ") + "; ".join(problems),
                           params={"problems": problems, "reason": "display_name"})
    lk = _lock(svc)
    try:
        lib = _lib(svc)
        gen = _generation_info(draft)
        if replace_equipment_id:
            if replace_equipment_id not in lib.ids():
                raise BackendError("NOT_FOUND", tr(f"差し替える装備がありません: {replace_equipment_id}",
                                                   f"The equipment to replace was not found: {replace_equipment_id}"))
            eq = replace_converted(lib, replace_equipment_id, draft.convert_dir, source_name="model")
            if name_en:
                # rename は装備をファイルから読み直して保存するので、先に行ってから読み直した装備に生成の記録を入れる
                # （逆の順では、メモリ上で更新した generation が捨てられ、古いプロンプト・シードが残った。点検 N02）
                lib.rename(eq.id, name_en=name_en, name_ja=name_ja if name_ja is not None else None)
                eq = lib.load(eq.id)
            eq.data["generation"].update({k: v for k, v in gen.items() if k != "input_image" or v})
            if "candidate_choice" not in gen:            # 前の生成で選んだ候補の記録を残さない（SF06）
                eq.data["generation"].pop("candidate_choice", None)
            if "variant_of" not in gen:                  # 前の見た目の作り直しの記録を残さない（SF10）
                eq.data["generation"].pop("variant_of", None)
        else:
            eq = import_converted(lib, draft.convert_dir, name_en, name_ja or None, source_name="model", generation=gen)
        try:
            src = os.path.join(eq.dir, "source")
            os.makedirs(src, exist_ok=True)
            files = draft.files()
            for key, name in PREVIEW_FILES.items():
                if key == "preview_png":
                    continue                         # modbuilder が source/preview.png に入れる
                dst = os.path.join(src, name)
                if key in files:
                    shutil.copyfile(files[key], dst)
                elif os.path.isfile(dst):
                    os.remove(dst)                   # 再生成で無くなったもの（候補の図など）
            g = eq.data["generation"]
            dinp = draft.data.get("input") or {}
            recs = dinp.get("images") or ([dinp["image"]] if isinstance(dinp.get("image"), dict) else [])   # 以前の下書きは 1 枚
            for f in os.listdir(src):                           # 前の入力画像（再生成で枚数が変わったときなど）
                if f.startswith("input") and f.endswith(".png"):
                    os.remove(os.path.join(src, f))
            if g.get("input_image"):
                g["input_image"]["stored_path"] = None
            for r in g.get("input_images") or []:
                r["stored_path"] = None
            if keep_image and recs:
                # 全ての入力画像を保存する（再生成に使う）。1 枚目は以前の形式どおり source/input.png にも置く
                for i, r in enumerate(recs, 1):
                    srcp = draft.path(*r["file"].split("/"))
                    if not os.path.isfile(srcp):
                        continue
                    name = f"input_{i}.png"
                    shutil.copyfile(srcp, os.path.join(src, name))
                    if i <= len(g.get("input_images") or []):
                        g["input_images"][i - 1]["stored_path"] = "source/" + name
                    if i == 1:
                        shutil.copyfile(srcp, os.path.join(src, "input.png"))
                        g["input_image"] = {**(g.get("input_image") or {}), "stored_path": "source/input.png"}
            g["source_glb"] = {"path": "source/generated.glb"} if "generated_glb" in files else g.get("source_glb")
            # 変換の前に回した角度（0 / 180。再生成の差し替えでも毎回書く）。generated.glb は回す前の生成結果のまま
            eq.data["conversion"]["yaw_deg"] = int((draft.data.get("conversion") or {}).get("yaw_deg") or 0)
            lib.save(eq)
        except BaseException as e:
            if replace_equipment_id:
                raise
            # 新しい装備の保存の後半（source への写し・最後の保存）で失敗したら、作った装備を消す（残すと下書きは ready のままで、
            # 保存し直すと同じ下書きから 2 件目の装備ができた。2026-10-27 検証 X6X-02）
            _discard_new_equipment(eq)
            if isinstance(e, OSError):
                full = e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112)
                raise BackendError("INVALID_INPUT", tr(f"装備の保存に失敗しました（ファイルを書けません: {type(e).__name__}: {e}）。"
                                                       "装備は作られていません。空きを作るなどしてから、もう一度保存してください",
                                                       f"Could not save the equipment (cannot write files: {type(e).__name__}: {e}). "
                                                       "The equipment was not created. Free up space or fix the cause, then save "
                                                       "again"),
                                   params={"reason": "save_copy", "cause": "disk_full" if full else "os_error"}) from e
            raise
        logs.info(tr(f"下書き {draft_id} を装備 {eq.id} として保存", f"Saved draft {draft_id} as equipment {eq.id}")
                  + (tr("（再生成の差し替え）", " (replaced by regeneration)") if replace_equipment_id else ""))
    finally:
        lk.release()
    if not s["keep_drafts"]:
        svc.drafts.delete(draft_id)
    else:
        # 残した下書きに保存済みの印を付ける（画面は起動時にこれを「保存していない下書き」として復元しない。データの整理で
        # 消せる。同じ下書きをもう一度保存したら新しい装備で上書きする。点検 R18A-02）
        draft.data["saved"] = {"equipment_id": eq.id, "at": logs.now_iso(), "replaced": bool(replace_equipment_id)}
        try:
            svc.drafts.save(draft)
        except OSError as e:
            # 装備はもう保存してある。印の書き込みの失敗（ディスクの空き・権限・ウイルス対策のロック）を保存の失敗として返すと、
            # 利用者が保存し直して同じ下書きから 2 件目の装備ができた（X6X-02 で塞いだ重複が戻る）。注意をログに残して成功を返す
            # （2026-10-27 検証 GX R18A-02 の再点検）
            logs.warning(tr(f"下書き {draft_id} に保存済みの印を書けませんでした（{type(e).__name__}: {e}）。装備 {eq.id} は"
                            "保存してあります。次の起動でこの下書きが未保存として出たら、保存し直さずに破棄してください"
                            "（生成の画面の「破棄」で消せます）",
                            f"Could not mark draft {draft_id} as saved ({type(e).__name__}: {e}). The equipment {eq.id} "
                            "has been saved. If this draft is shown as unsaved at the next start, discard it instead of "
                            "saving it again (use \"Discard\" on the Generate page)"))
    from modbuilder.errors import LibraryError
    try:
        view = equipment_view(lib.load(eq.id))
    except (OSError, LibraryError) as e:
        # 保存した直後の読み直しの失敗（ウイルス対策のロックなど）も保存の失敗にしない（同じ理由。保存した内容から返す）
        logs.warning(tr(f"保存した装備 {eq.id} を読み直せませんでした（{type(e).__name__}）。保存した内容を返します",
                        f"Could not reload the saved equipment {eq.id} ({type(e).__name__}). Returning the saved data"))
        view = equipment_view(eq)
    return {**view, "warnings": _name_warnings(name_en)}


# ---------------------------------------------------------------------------
# 保存済みの装備を変換し直す（3D 生成はやり直さない。B-001 の修正のような変換側の改善を、保存した装備に当てる）
# ---------------------------------------------------------------------------
def summarize_reconvert(p: dict) -> dict:
    return {"equipment_id": p.get("equipment_id"), "yaw_deg": p.get("yaw_deg")}


def _converted_texture_max(eq) -> int:
    """今のアセットを作った変換のテクスチャの最大辺（保存した変換の報告 ``source\\conversion_report.json`` の
    config.texture_max_size）。報告が無い・読めない・アプリで選べない値のときは 2048（SF24 より前の変換の値）。"""
    import json
    from .settings import CONVERT_TEXTURE_MAX, GENERATION_DEFAULTS
    default = int(GENERATION_DEFAULTS["convert_texture_max"])
    rel = (eq.data.get("conversion") or {}).get("report")
    if not rel:
        return default
    try:
        with open(os.path.join(eq.dir, *str(rel).replace("\\", "/").split("/")), encoding="utf-8") as f:
            v = ((json.load(f) or {}).get("config") or {}).get("texture_max_size")
        v = int(v)
    except (OSError, ValueError, TypeError, AttributeError):
        return default
    return v if v in CONVERT_TEXTURE_MAX else default


def _reconvert_mark(eq) -> tuple:
    """「変換し直す」の開始から保存までに装備が差し替えられていないかの印: 生成結果（source\\generated.glb）の SHA-256 と、
    アセットの組（名前・SHA-256）。名前の変更・表示文の編集では変わらない（2026-10-27 検証 X6X-01）。"""
    from modbuilder.library import sha256_file
    try:
        glb = sha256_file(os.path.join(eq.dir, "source", "generated.glb"))
    except OSError:
        glb = None
    a = eq.data.get("assets") if isinstance(eq.data.get("assets"), dict) else {}
    return glb, sorted((str(f.get("file")), str(f.get("sha256"))) for f in a.get("files") or [] if isinstance(f, dict))


def run_reconvert_equipment(svc, ctx, params: dict) -> dict:
    """ジョブ ``reconvert_equipment``: 装備に保存してある生成結果（``source\\generated.glb``）を今の gdconvert で変換し直す。

    内部ID・レコードのパス・タグ名・表示名・生成の記録（プロンプト・シード・入力画像）はそのまま。アセット（.msh・.tex）、
    プレビュー用の GLB（男女）・確認画像・変換の記録（品質の注意）を差し替える。MOD の状態は、ビルド済み・導入済みだったなら
    「作成が必要」（not_built）になる（ゲームに反映するには MOD を作成し直して導入する）。向き（``yaw_deg``）は省くと保存時と同じ。
    変換の作業は一時的な下書きのフォルダーで行い、失敗・取消のときは装備を変えない。"""
    import copy
    from modbuilder.importers import replace_converted
    from . import pipeline
    from .settings import validate_generation

    eq_id = str(params.get("equipment_id") or "")
    lib = _wrap(_lib)(svc)
    if eq_id not in lib.ids():
        raise BackendError("NOT_FOUND", tr(f"装備がありません: {eq_id}", f"Equipment not found: {eq_id}"))
    eq = lib.load(eq_id)
    glb = os.path.join(eq.dir, "source", "generated.glb")
    if not os.path.isfile(glb):
        raise BackendError("INVALID_INPUT", tr("この装備には生成結果（source\\generated.glb）が保存されていないため、変換し直せません",
                                               "This equipment cannot be reconverted because no generation result "
                                               "(source\\generated.glb) is saved with it"),
                           params={"reason": "no_generated_glb", "equipment_id": eq_id})
    mark = _reconvert_mark(eq)
    s = svc.settings.get()
    saved = (eq.data.get("generation") or {}).get("settings") or {}
    # 変換の設定は保存したときの値（目標頂点数・テクスチャの最大辺 SF24）。目標頂点数が記録に無い古い装備は今の設定。
    # 最大辺が記録に無い装備（SF24 より前の装備）は今の設定ではなく、今のアセットを作った変換の値（無ければ 2048）
    keep = {k: saved[k] for k in ("target_vertices", "convert_texture_max") if saved.get(k)}
    keep.setdefault("convert_texture_max", _converted_texture_max(eq))
    gen = validate_generation({**s["generation"], **keep, **(params.get("settings") or {})})
    conv0 = eq.data.get("conversion") or {}
    yaw = pipeline.validate_yaw(params["yaw_deg"] if params.get("yaw_deg") is not None else conv0.get("yaw_deg") or 0)
    gd, _w = pipeline.preflight(svc, "glb", gen, ctx)
    g0 = eq.data.get("generation") or {}
    draft = svc.drafts.create("reconvert", g0.get("provider"), gen, replace_equipment_id=eq_id, job_id=ctx.job.id)
    before = (eq.data.get("mod") or {}).get("status")
    try:
        dst = os.path.join(draft.generate_dir, "generated.glb")
        shutil.copyfile(glb, dst)
        draft.data["generation"] = {"glb": "generate/generated.glb"}
        draft.data.update(status="reconverting", seed=g0.get("seed"), model_id=g0.get("model_id"))
        svc.drafts.save(draft)
        prev_warn = ", ".join(conv0.get("warnings") or []) or tr("なし", "none")
        ctx.info(tr(f"装備 {eq_id} を保存してある生成結果から変換し直します（向き {yaw}°、目標頂点数 {gen['target_vertices']}。"
                    f"前の注意: {prev_warn}）",
                    f"Reconverting equipment {eq_id} from the saved generation result (direction {yaw}°, target vertex count "
                    f"{gen['target_vertices']}; previous warnings: {prev_warn})"))
        pipeline._convert(svc, ctx, draft, dst, gd, gen, (0.02, 0.95), yaw_deg=yaw)
        ctx.check_cancel()
        ctx.progress(0.96, "reconvert_save")
        lk = _lock_for_job(svc, ctx, 0.96, "reconvert_wait_mod", "reconvert_save")
        try:
            lib = _wrap(_lib)(svc)
            cur = lib.load(eq_id)
            if _reconvert_mark(cur) != mark:
                # 変換の間に再生成の保存などで装備が差し替えられた。古い生成結果の変換で上書きしない（2026-10-27 検証 X6X-01）
                raise BackendError("INVALID_INPUT", tr("変換し直しの間にこの装備が差し替えられたため（再生成の保存など）、変換の結果を"
                                                       "保存しませんでした。必要ならもう一度「変換し直す」を行ってください",
                                                       "The equipment was replaced while it was being reconverted (for example, "
                                                       "a regenerated result was saved), so the result was not saved. Reconvert "
                                                       "it again if needed"),
                                   params={"reason": "equipment_changed", "equipment_id": eq_id})
            orig_gen = copy.deepcopy(cur.data.get("generation") or {})
            eq = _wrap(replace_converted)(lib, eq_id, draft.convert_dir, source_name="model")
            eq.data["generation"] = orig_gen          # 生成の記録は変えない（変換の作業フォルダーの記録で上書きしない）
            src = os.path.join(eq.dir, "source")
            files = draft.files()
            for key in ("preview_m_glb", "preview_f_glb"):
                if key in files:
                    shutil.copyfile(files[key], os.path.join(src, PREVIEW_FILES[key]))
            eq.data["conversion"]["yaw_deg"] = yaw
            eq.data["conversion"]["reconverted_at"] = logs.now_iso()
            lib.save(eq)
        finally:
            lk.release()
    finally:
        svc.drafts.delete(draft.id)
    view = equipment_view(lib.load(eq_id))
    warns = ", ".join(view["conversion_warnings"]) or tr("なし", "none")
    logs.info(tr(f"装備 {eq_id} を変換し直しました（注意: {warns}、MOD の状態 {before} → {view['mod_detail'].get('status')}）",
                 f"Reconverted equipment {eq_id} (warnings: {warns}; MOD status {before} → {view['mod_detail'].get('status')})"),
              job=ctx.job.id)
    return {"equipment": view, "previous_warnings": conv0.get("warnings") or [], "mod_status_before": before}
