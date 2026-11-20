"""テキスト入力の補助（SF07。2026-10-17 U-A4）: 生成 AI が読む長さ（CLIP のトークン数）の見積もりと、英語以外の語の検出。

生成 AI（microsoft/TRELLIS-text-xlarge）は、文章を openai/clip-vit-large-patch14 のトークナイザーで 77 トークン（開始・
終了の 2 つを含む）までに切って読む。生成側（generate_glb.py）は C17 の ``scripts/trellis/prompt_fit.py``（fit_prompt）で
「武器なし」（D-013）の指定を上限の中に残す。ここでは同じ fit_prompt を使い、生成の前に画面へ「何トークンか・後ろの
どこが読まれないか」を出す。生成の処理・生成に渡す文章は変えない（表示のための見積もりだけ）。

数え方:

* CLIP（テキスト用のモデル）を取得済みで、注意文に同意済み（D-011）のときは、取得済みの ``tokenizer.json`` を
  オフラインで読み、同じ規則（NFC・空白の正規化・小文字化 → 文字の種類での区切り → バイト単位の BPE）で数える
  （``exact``: True）。新しい取得はしない。transformers・tokenizers は使わない（バックエンドの依存を増やさない）。
  BPE は公開されている標準の手順を、ここで書いたもの（他のコードの写しではない）。
* それ以外（未取得・未同意・ファイルの照合値が違う）は、文字の種類ごとのおおよその数（``exact``: False、概算）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
import unicodedata

from . import paths
from .errors import BackendError
from .i18n import tr

CLIP_MAX_TOKENS = 77
# 見積もりで受け付ける長さ（生成の上限 pipeline.MAX_TEXT = 1000 文字より長い文章も数えるが、際限なくは数えない）
MAX_ESTIMATE_CHARS = 5000
_BOS, _EOS = "<|startoftext|>", "<|endoftext|>"
_CONTRACTIONS = ("s", "t", "re", "ve", "m", "ll", "d")      # 'xx の形の語尾（tokenizer.json の区切りの規則と同じ順）
_EOW = "</w>"


# --- 文字の種類と、区切り（tokenizer.json の pre_tokenizer の規則） ------------------------------------------------
def _is_letter(c: str) -> bool:
    return unicodedata.category(c)[0] == "L"


def _is_number(c: str) -> bool:
    return unicodedata.category(c)[0] == "N"


def _is_space(c: str) -> bool:
    # Python の isspace は区切りの制御文字 U+001C〜U+001F も空白とするが、tokenizer.json の \s は空白としない
    # （実行環境の CLIPTokenizerFast で確かめた。他の空白 U+0085・U+00A0・U+3000 などは同じ）
    return c.isspace() and not "\x1c" <= c <= "\x1f"


def _normalize(text: str) -> str:
    """NFC → 連続する空白を 1 つの半角スペースに → 小文字（tokenizer.json の normalizer）。"""
    t = unicodedata.normalize("NFC", text)
    out, prev_space = [], False
    for c in t:
        if _is_space(c):
            if not prev_space:
                out.append(" ")
            prev_space = True
        else:
            out.append(c)
            prev_space = False
    return "".join(out).lower()


def pre_tokens(text: str) -> list[str]:
    """正規化した文章の区切り: 's 't 're 've 'm 'll 'd / 文字の並び / 数字 1 つ / 空白・文字・数字以外の並び（空白は捨てる）。"""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            hit = next((s for s in _CONTRACTIONS if text.startswith(s, i + 1)), None)
            if hit:
                out.append("'" + hit)
                i += 1 + len(hit)
                continue
        if _is_letter(c):
            j = i + 1
            while j < n and _is_letter(text[j]):
                j += 1
        elif _is_number(c):
            j = i + 1
        elif _is_space(c):
            i += 1
            continue
        else:
            j = i + 1
            while j < n and not (_is_letter(text[j]) or _is_number(text[j]) or _is_space(text[j])):
                j += 1
        out.append(text[i:j])
        i = j
    return out


def _byte_alphabet() -> list[str]:
    """バイト（0〜255）→ 1 文字の対応（バイト単位の BPE の標準の対応。表示できる文字はそのまま、他は 256 以降の文字）。"""
    keep = [*range(ord("!"), ord("~") + 1), *range(ord("¡"), ord("¬") + 1), *range(ord("®"), ord("ÿ") + 1)]
    table, extra = {}, 0
    for b in range(256):
        if b in keep:
            table[b] = chr(b)
        else:
            table[b] = chr(256 + extra)
            extra += 1
    return [table[b] for b in range(256)]


_BYTE_CHARS = _byte_alphabet()


class ClipBpe:
    """CLIP のトークナイザー（バイト単位の BPE、語の終わりに </w>）で数える。vocab: 文字列 → id、merges: 結合の順。"""

    def __init__(self, vocab: dict[str, int], merges: list[tuple[str, str]]):
        self.vocab = vocab
        self.ranks = {pair: i for i, pair in enumerate(merges)}
        self._cache: dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_tokenizer_json(cls, data: dict) -> "ClipBpe":
        model = data.get("model") or {}
        if model.get("type") != "BPE" or model.get("end_of_word_suffix") != _EOW:
            raise ValueError("tokenizer.json is not CLIP's BPE")
        merges = []
        for m in model.get("merges") or []:
            pair = tuple(m.split(" ", 1)) if isinstance(m, str) else tuple(m)
            if len(pair) != 2:
                raise ValueError(f"bad merge: {m!r}")
            merges.append(pair)
        return cls(dict(model.get("vocab") or {}), merges)

    def _bpe_len(self, piece: str) -> int:
        """1 つの区切り（正規化済み）の BPE のトークン数。"""
        with self._lock:
            hit = self._cache.get(piece)
        if hit is not None:
            return hit
        chars = [_BYTE_CHARS[b] for b in piece.encode("utf-8")]
        word = chars[:-1] + [chars[-1] + _EOW]
        while len(word) > 1:
            best, best_rank = None, None
            for k in range(len(word) - 1):
                r = self.ranks.get((word[k], word[k + 1]))
                if r is not None and (best_rank is None or r < best_rank):
                    best, best_rank = (word[k], word[k + 1]), r
            if best is None:
                break
            merged, k = [], 0
            while k < len(word):
                if k < len(word) - 1 and (word[k], word[k + 1]) == best:
                    merged.append(word[k] + word[k + 1])
                    k += 2
                else:
                    merged.append(word[k])
                    k += 1
            word = merged
        n = len(word)
        with self._lock:
            if len(self._cache) > 50000:
                self._cache.clear()
            self._cache[piece] = n
        return n

    def count(self, text: str) -> int:
        """開始・終了の 2 トークンを含む数（TRELLIS の encode_text が切る前の数。generate_glb.py の数え方と同じ）。"""
        n = 2
        for i, raw in enumerate(text.split(_EOS)):     # <|endoftext|> は正規化の前に 1 トークン
            if i:
                n += 1
            for j, part in enumerate(_normalize(raw).split(_BOS)):     # <|startoftext|> は正規化の後に 1 トークン
                if j:
                    n += 1
                n += sum(self._bpe_len(p) for p in pre_tokens(part))
        return n


# --- 取得済みのトークナイザー（オフライン。同意済みのときだけ） -----------------------------------------------------
_loaded: dict[str, tuple[tuple, ClipBpe]] = {}
_load_lock = threading.Lock()


def _fetch_constants():
    """CLIP の固定（リポジトリ・コミット）と、tokenizer.json の大きさ・SHA-256（models_manifest_text.json）。"""
    from . import consent
    fm = consent._fetch_models()
    with open(os.path.join(paths.TRELLIS_SCRIPTS, "models_manifest_text.json"), encoding="utf-8") as f:
        man = json.load(f)
    info = ((man.get("clip") or {}).get("files") or {}).get("tokenizer.json") or {}
    return fm.CLIP_REPO, fm.CLIP_REVISION, info.get("size"), info.get("sha256")


def tokenizer_path(root: str) -> str:
    repo, rev, _, _ = _fetch_constants()
    return os.path.join(root, "hf-home", "hub", "models--" + repo.replace("/", "--"), "snapshots", rev, "tokenizer.json")


def load_tokenizer(root: str | None) -> tuple[ClipBpe | None, str]:
    """-> (トークナイザー, 理由)。理由: ok / no_root / no_consent / missing / mismatch / unreadable。"""
    if not root:
        return None, "no_root"
    from . import consent
    try:
        if not consent.status(root)["accepted"]:
            return None, "no_consent"
        _, _, size, sha = _fetch_constants()
        p = tokenizer_path(root)
        st = os.stat(p)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, KeyError, AttributeError):
        return None, "unreadable"
    key = (st.st_size, st.st_mtime_ns)
    with _load_lock:
        hit = _loaded.get(p)
        if hit and hit[0] == key:
            return hit[1], "ok"
        try:
            with open(p, "rb") as f:
                data = f.read()
            if (size is not None and len(data) != size) or (sha and hashlib.sha256(data).hexdigest() != sha):
                return None, "mismatch"
            bpe = ClipBpe.from_tokenizer_json(json.loads(data.decode("utf-8")))
        except (OSError, ValueError, UnicodeDecodeError):
            return None, "unreadable"
        _loaded[p] = (key, bpe)
        return bpe, "ok"


# --- 概算（トークナイザーが使えないとき） -------------------------------------------------------------------------
def approx_count(text: str) -> int:
    """おおよその数（開始・終了を含む）。英字の語は 1〜数トークン、数字は 1 文字 1 トークン、かな・漢字など英字以外は
    1 文字およそ 2 トークン（UTF-8 で 3 バイト）、記号の並びは 1 文字 1 トークン。"""
    n = 2
    for p in pre_tokens(_normalize(text)):
        if p.isascii():
            n += -(-len(p) // 7) if p[0].isalpha() else len(p) if not p[0].isdigit() else 1
        else:
            n += max(1, round(len(p.encode("utf-8")) * 2 / 3))
    return n


# --- 英語以外の語 ---------------------------------------------------------------------------------------------------
def foreign_words(text: str, limit: int = 5) -> list[str]:
    """英字（ASCII）以外の文字・数字を含む語（空白・句読点で区切った塊。かな・漢字・全角の英数字・é など）を最大 limit 件。"""
    out: list[str] = []
    word = []

    def flush():
        w = "".join(word).strip()
        if w and any(ord(c) > 127 and (_is_letter(c) or _is_number(c)) for c in w) and w not in out:
            out.append(w)
        word.clear()

    for c in text:
        if c.isspace() or c in ",.;:!?、。，．！？「」『』（）()[]【】":
            flush()
        else:
            word.append(c)
        if len(out) >= limit:
            return out
    flush()
    return out[:limit]


# --- 見積もり（POST /prompt/estimate） --------------------------------------------------------------------------------
_pf = None


def _prompt_fit():
    """scripts/trellis/prompt_fit.py（C17。生成側が 77 トークンに収めるときと同じ関数）。"""
    global _pf
    if _pf is None:
        spec = importlib.util.spec_from_file_location("gdskin_prompt_fit", os.path.join(paths.TRELLIS_SCRIPTS, "prompt_fit.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _pf = mod
    return _pf


def _kept_prefix_len(body: str, count, limit: int) -> int:
    """上限に収まる先頭の長さ（「武器なし」を足さないとき。CLIP が切る位置の目安）。

    fit_prompt と同じ切り方（指定を足さずに数える）: 語の境目（空白・句読点）で収まる最長の位置。境目で切ると
    ずっと多く（_CHAR_CUT_MIN_GAIN トークン以上）落ちるとき、または境目で収まる位置が無いとき（読点の無い日本語・
    ハイフンでつないだ長い英語）は、その後ろの文字の並びの中で収まる最長の位置（UA4-R2）。
    """
    pf = _prompt_fit()
    memo: dict[int, tuple | None] = {}

    def at(cut: int):
        if cut not in memo:
            part = pf._TRAILING_RE.sub("", body[:cut])
            memo[cut] = (cut, part, count(part)) if part.strip() else None
        return memo[cut]

    bounds = sorted({b.start() for b in pf._BOUNDARY_RE.finditer(body) if b.start() > 0})
    word = pf._longest_fit(bounds, at, limit)
    lower = word[0] if word else 0
    upper = next((b for b in bounds if b > lower), len(body))
    char = pf._longest_fit(range(lower + 1, upper), at, limit)
    if word and (char is None or char[2] - word[2] < pf._CHAR_CUT_MIN_GAIN):
        return word[0]
    return char[0] if char else 0


def estimate(text, root: str | None, allow_weapons: bool = False) -> dict:
    """文章（生成と同じく前後の空白を除く）→ 生成 AI が読むトークン数の見積もり。

    tokens: 生成に使う文章のトークン数（「武器なし」の指定を足した後、収まらないときは切った後。上限を超えるのは
    --allow-weapons で切り捨てだけのとき）、original: 利用者の文章だけの数、with_suffix: 指定を足したときの切る前の数
    （足さないときは None）、limit: 77、suffix_added: 「武器なし」の指定を足す、truncated: 利用者の文章の一部が
    読まれない、dropped_text: 読まれない後ろの部分（目安）、exact: CLIP のトークナイザーで数えた（False は概算）、
    method: clip_tokenizer / approx、reason: 概算の理由、notes: pipeline.text_notes のコード、foreign_words: 英語以外の
    文字を含む語、no_weapon_read: CLIP が読む範囲に「武器なし」の指定がある。
    """
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise BackendError("INVALID_INPUT", tr("text は文字列で指定してください", "Specify text as a string"))
    if len(text) > MAX_ESTIMATE_CHARS:
        raise BackendError("INVALID_INPUT", tr(f"文章が長すぎます（{len(text)} 文字）", f"The text is too long ({len(text)} characters)"),
                           params={"reason": "text_too_long"})
    from . import pipeline
    if not pipeline.utf8_ok(text):       # 対になっていないサロゲートは生成と同じく入力の誤り（以前は INTERNAL。点検 R10A-04）
        raise pipeline.text_invalid_chars()
    t = text.strip()
    bpe, reason = load_tokenizer(root)
    count = bpe.count if bpe else approx_count
    out = {"limit": CLIP_MAX_TOKENS, "exact": bpe is not None, "method": "clip_tokenizer" if bpe else "approx",
           "reason": None if bpe else reason, "notes": pipeline.text_notes(t) if t else [], "foreign_words": foreign_words(t)}
    if not t:
        return {**out, "tokens": 0, "original": 0, "with_suffix": None, "suffix_added": False, "truncated": False,
                "dropped_text": None, "no_weapon_read": False}
    pf = _prompt_fit()
    used, info = pf.fit_prompt(t, count, limit=CLIP_MAX_TOKENS, add_suffix=not allow_weapons)
    pt = info["prompt_tokens"]
    suffix_added = "with_suffix" in pt          # fit_prompt が「武器なし」の指定を足した（足すときだけ記録する）
    dropped = None
    if info["prompt_truncated"]:
        if suffix_added:
            # 足した指定の前に残った利用者の文章（指定だけが残ったときは空）
            kept = used[: -len(pf.NO_WEAPON_SUFFIX)] if used.endswith(pf.NO_WEAPON_SUFFIX) else ""
            body = t.rstrip(" .,")
            dropped = body[len(kept):] if body.startswith(kept) else None
        else:
            dropped = t[_kept_prefix_len(t, count, CLIP_MAX_TOKENS):]
        dropped = (dropped or "").strip(" ,.;:、。，．\t\r\n") or None
    return {**out, "tokens": pt["used"], "original": pt["original"], "with_suffix": pt.get("with_suffix"),
            "suffix_added": suffix_added,
            "truncated": bool(info["prompt_truncated"]), "dropped_text": dropped,
            "no_weapon_read": bool(info.get("no_weapon_kept"))}
