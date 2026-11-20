"""D-011: テキスト→3D に使う CLIP の注意文と、同意の記録。

注意文のファイル・記録の形式は ``scripts/trellis/fetch_models.py``（``ensure_clip_consent``）と同じものを使う
（``<TRELLIS 実行環境>\\consents.json``、注意文の SHA-256 が変わったら同意し直し）。記録するのは、画面で注意文を表示して
利用者が「同意する」を押したときだけ（``method`` は ``app``）。

注意文は日本語（``clip_model_card_notice.ja.txt``）と英語（``.en.txt``）。画面は表示言語の注意文を出し（英語の画面は en、
ほかは ja）、同意の記録には表示した注意文の言語（``notice_lang``）と、その文の SHA-256 を残す。同意は、記録した言語の今の
注意文と SHA-256 が合う間は有効（どちらの言語で同意しても同じ）。``notice_lang`` の無い記録（2026-10-18 より前）は
日本語の注意文への同意として、今までどおり日本語の注意文の SHA-256 と比べる（2026-10-18 未決定 #2）。
"""
from __future__ import annotations

import importlib.util
import json
import os

from . import logs, paths
from .errors import BackendError
from .i18n import tr

_fm = None


def _fetch_models():
    """scripts/trellis/fetch_models.py の定数（モデルとコミット、注意文の場所）と、同意の判定を読む。"""
    global _fm
    if _fm is None:
        spec = importlib.util.spec_from_file_location("gdskin_fetch_models",
                                                      os.path.join(paths.TRELLIS_SCRIPTS, "fetch_models.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _fm = mod
    return _fm


def normalize_lang(lang: str | None) -> str:
    """表示言語 → 注意文の言語（"en"・"en-US" → en、ほか（空・ja・不明）→ ja。画面の L.NormalizeLanguage と同じ規則）。"""
    return _fetch_models().notice_lang(lang)


def notice_text(lang: str | None = "ja") -> tuple[str, str]:
    """-> (注意文, その文の SHA-256)。"""
    _, text, digest = _fetch_models().notice(lang)
    return text, digest


def lang_of_digest(notice_sha256: str) -> str | None:
    """SHA-256 が今のどの言語の注意文のものか（どれでもなければ None）。"""
    fm = _fetch_models()
    return next((lang for lang in fm.NOTICE_FILES if fm.notice(lang)[2] == notice_sha256), None)


def _path(root: str) -> str:
    return os.path.join(root, "consents.json")


def status(root: str | None, lang: str | None = "ja") -> dict:
    """同意の状態と、表示言語（lang）の注意文。accepted は、どちらの言語の同意でも記録が今の注意文に合えば true。"""
    fm = _fetch_models()
    lang = normalize_lang(lang)
    text, digest = notice_text(lang)
    out = {"notice": text, "notice_sha256": digest, "notice_lang": lang, "accepted": False, "accepted_at": None,
           "method": None, "accepted_lang": None, "root_found": bool(root)}
    if root and os.path.isfile(_path(root)):
        try:
            with open(_path(root), encoding="utf-8") as f:
                rec = json.load(f).get(fm.CONSENT_KEY)
        except (OSError, ValueError, AttributeError):
            rec = None
        if fm.consent_valid(rec):
            out.update(accepted=True, accepted_at=rec.get("accepted_at"), method=rec.get("method"),
                       accepted_lang=fm.record_lang(rec))
    return out


def accept(root: str | None, notice_sha256: str, method: str = "app", lang: str | None = None) -> dict:
    """画面で表示した注意文（の SHA-256）に同意したことを記録する（method: app = 生成画面、app-setup = セットアップ画面）。

    lang は表示した注意文の言語（ja / en）。その言語の今の注意文の SHA-256 と合わなければ断る。省略したときは、SHA-256 が
    合う言語の注意文への同意として記録する（以前の呼び出し方。どれにも合わなければ断る）。
    """
    if not root:
        raise BackendError("LIBRARY_MISSING", tr("TRELLIS 実行環境が見つからないため、同意を記録できません",
                                                 "The acceptance cannot be recorded because the TRELLIS runtime was "
                                                 "not found"))
    fm = _fetch_models()
    shown = normalize_lang(lang) if lang else lang_of_digest(notice_sha256)
    if shown is None or notice_sha256 != notice_text(shown)[1]:
        raise BackendError("INVALID_INPUT", tr("表示した注意文が最新ではありません。もう一度表示してから同意してください",
                                               "The notice shown is not the latest one. Show it again, then accept it"))
    p = _path(root)
    os.makedirs(root, exist_ok=True)
    records = {}
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                records = json.load(f)
        except ValueError:           # 壊れた・途中で切れた・BOM 付きの JSON（status は同意なしとして扱う。点検 R11A-02）
            records = None
        if not isinstance(records, dict):
            # 記録は CLIP の同意だけなので、作り直しても失うものは無い（読めないままでは同意し直せない）
            logs.warning(tr(f"consents.json が読めないため、同意の記録を作り直します: {p}",
                            f"consents.json cannot be read, so the record of acceptance is created again: {p}"))
            records = {}
    records[fm.CONSENT_KEY] = fm.consent_record(shown, method)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)
    return status(root, shown)
