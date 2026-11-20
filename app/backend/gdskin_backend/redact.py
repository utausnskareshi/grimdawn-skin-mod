"""ログに個人情報・入力の内容を残さないための置き換え（docs/app_architecture.md 8.3 節）。

* 利用者のフォルダー（``C:\\Users\\<名前>``）→ ``%USERPROFILE%``（``\\``・``/``・JSON の ``\\\\``・``\\uXXXX`` の書き方すべて）
* 登録した秘密の文字列（プロンプト、入力画像・GLB のファイル名とパス）→ ``<input>``（装備名は登録しない。アプリのログには
  装備 ID を書く。8.3 節）
* プロンプトや画像は :func:`fingerprint` の「文字数・SHA-256 の先頭」だけを記録する
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading

from .i18n import tr

_lock = threading.Lock()
_secrets: dict[str, int] = {}       # 文字列 → 登録数（同じ文字列を複数のジョブが登録しても消し忘れないように）
_MIN_SECRET = 3                      # 短すぎる文字列（"a" など）は置き換えると読めなくなるので登録しない


def _home_variants(bases=None) -> list[str]:
    """利用者のフォルダーの書き方: そのまま・「/」区切り・JSON の「\\\\」、JSON の ensure_ascii の形（全角の名前が
    ``\\uXXXX`` になる。``\\\\`` と「/」区切りの両方）。"""
    if bases is None:
        bases = {os.path.expanduser("~"), os.environ.get("USERPROFILE", "")}
    out = []
    for base in set(bases):
        if not base or len(base) < 4:
            continue
        out += [base, base.replace("\\", "/"), base.replace("\\", "\\\\"),
                json.dumps(base)[1:-1], json.dumps(base.replace("\\", "/"))[1:-1]]
    return sorted(set(out), key=len, reverse=True)


def _home_regex(variants: list[str]):
    return re.compile("|".join(re.escape(h) for h in variants), re.IGNORECASE) if variants else None


_HOME = _home_variants()
_HOME_RE = _home_regex(_HOME)
# 既定の場所以外の利用者フォルダー（別ユーザー・別ドライブの C:\Users\<名前>）。区切りは「\」「/」と JSON の「\\」で、
# 最短に取る（JSON の \uXXXX の「\」を区切りとして飲み込まない）。名前は \uXXXX も含めて最後まで置き換える。
# 名前に空白は含めない（説明の文まで消えるため。空白を含む名前は入力のパスとして秘密の文字列で置き換わる）
_USERS_RE = re.compile(r"([A-Za-z]:(?:\\\\|[\\/])+?Users(?:\\\\|[\\/])+?)((?:\\u[0-9A-Fa-f]{4}|[^\\/\s\"'])+)", re.IGNORECASE)


def add_secret(text: str | None) -> None:
    if not text or len(text.strip()) < _MIN_SECRET:
        return
    with _lock:
        for t in {text, text.strip()}:
            _secrets[t] = _secrets.get(t, 0) + 1


def remove_secret(text: str | None) -> None:
    if not text:
        return
    with _lock:
        for t in {text, text.strip()}:
            n = _secrets.get(t, 0) - 1
            if n <= 0:
                _secrets.pop(t, None)
            else:
                _secrets[t] = n


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, "<input>")
        # JSON に書かれたときの形（\ が \\、" が \"）
        esc = s.replace("\\", "\\\\").replace('"', '\\"')
        if esc != s and esc in text:
            text = text.replace(esc, "<input>")
    if _HOME_RE is not None:
        text = _HOME_RE.sub("%USERPROFILE%", text)
    text = _USERS_RE.sub(lambda m: m.group(1) + "<user>" if m.group(2).lower() not in ("public", "default") else m.group(0), text)
    return text


def fingerprint(data: str | bytes) -> str:
    """同じ入力かを後で照合するための目印（中身は分からない）。"""
    b = data.encode("utf-8") if isinstance(data, str) else data
    n = tr(f"{len(data)} 文字", f"{len(data)} characters") if isinstance(data, str) else f"{len(b)} B"
    return f"{n}, sha256 {hashlib.sha256(b).hexdigest()[:12]}"
