"""表示言語（日本語・英語）ごとの文の選択（2026-10-24。docs/decisions.md D-060）。

バックエンドとその子プロセスが画面・ログに出す文は ``tr("日本語の文", "English text")`` と両方の言語で書き、
表示言語で選ぶ。表示言語は環境変数 ``GDSKIN_LANG``（WPF がバックエンドを起動するときに設定の表示言語を渡す。
子プロセスは環境変数を受け継ぐ）。"en"・"en-US" などは英語、ほか（未設定・"ja"・不明な値）は日本語
（画面の ``L.NormalizeLanguage`` と同じ規則）。呼ぶたびに環境変数を読む（テストで切り替えられるように）。

同じ規則の小さな関数を、単独で動く部品にも置く: ``src/gdconvert/_i18n.py``・``src/modbuilder/_i18n.py``・
``scripts/trellis/gen_helpers.py``（``tr``）・``scripts/trellis/fetch_models.py``（``notice_lang``）・
``app/backend/bg_preview.py``。日本語の文に対になる英語の文があることは ``tests/test_i18n_coverage.py`` が確かめる。
"""
from __future__ import annotations

import os

ENV = "GDSKIN_LANG"
LANGS = ("ja", "en")


def normalize(lang: str | None) -> str:
    """"en"・"en-US" → "en"、ほか（None・空・"ja"・不明）→ "ja"。"""
    s = (lang or "").strip().lower()
    return "en" if s == "en" or s.startswith("en-") else "ja"


def lang() -> str:
    """今の表示言語（"ja" / "en"）。"""
    return normalize(os.environ.get(ENV))


def tr(ja: str, en: str) -> str:
    """表示言語の文（英語なら en、ほかは ja）。"""
    return en if lang() == "en" else ja


def strip_end_period(text: str) -> str:
    """文の末尾の句点（「。」・「.」）と空白を外す。例外の文（日本語の Windows のエラーの文は「。」で終わる）を、句点や
    区切りが続く文の中に埋め込むときに使う（「…切断されました。。この取得の間は」のように重ねない。B-038）。"""
    return text.rstrip().rstrip("。.").rstrip()
