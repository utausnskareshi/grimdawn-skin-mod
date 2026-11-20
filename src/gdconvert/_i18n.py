"""表示言語（日本語・英語）ごとの文の選択（2026-10-24。docs/decisions.md D-060）。

エラー・注意・進行状況の文は ``tr("日本語の文", "English text")`` と両方の言語で書く。表示言語は環境変数
``GDSKIN_LANG``（アプリのバックエンドが受け継ぐ値）。"en"・"en-US" などは英語、ほかは日本語。
``app/backend/gdskin_backend/i18n.py`` と同じ規則（gdconvert は単独でも動くので、ここに置く）。
"""
from __future__ import annotations

import os


def lang() -> str:
    """今の表示言語（"ja" / "en"）。"""
    s = (os.environ.get("GDSKIN_LANG") or "").strip().lower()
    return "en" if s == "en" or s.startswith("en-") else "ja"


def tr(ja: str, en: str) -> str:
    """表示言語の文（英語なら en、ほかは ja）。"""
    return en if lang() == "en" else ja
