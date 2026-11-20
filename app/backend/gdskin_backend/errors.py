"""エラーコード（docs/app_architecture.md 8.1 節）と、子プロセスの出力からの分類。

画面に出す文は WPF のリソース（``Error_<code>_Title`` など）で引く。ここの ``message`` は技術的な説明で、
画面の「詳細」欄とログにだけ出す。
"""
from __future__ import annotations

import re

# code → HTTP の状態コード
CODES = {
    "GPU_NOT_FOUND": 503,
    "GPU_MEMORY_LOW": 503,
    "LIBRARY_MISSING": 503,
    "MODEL_MISSING": 503,
    "MODEL_LOAD_FAILED": 500,
    "GENERATION_FAILED": 500,
    "INVALID_MODEL": 422,
    "CONVERSION_FAILED": 500,
    "RIGGING_FAILED": 500,
    "EXPORT_FAILED": 500,
    "MOD_BUILD_FAILED": 422,
    "INSTALL_BLOCKED": 409,
    "GAME_RUNNING": 409,
    "GAME_NOT_FOUND": 404,
    "REFS_MISSING": 409,
    "CONSENT_REQUIRED": 403,
    "INVALID_INPUT": 400,
    "PACKAGE_INVALID": 422,     # 共有ファイル（.gdskin / .gdskinmod）が壊れている・改ざん・形式違い（params.reason）
    "DOWNLOAD_FAILED": 503,     # 初回セットアップの取得の失敗（通信・照合。params.reason = network / hash_mismatch）
    "SETUP_FAILED": 500,        # 初回セットアップの失敗（仮想環境の作成・パッケージの導入・動作確認。params.step）
    "DISK_SPACE_LOW": 507,      # 初回セットアップの取得先の空き容量が足りない
    "BUSY": 409,
    "NOT_CANCELLABLE": 409,
    "NOT_FOUND": 404,
    "CANCELLED": 409,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "INTERNAL": 500,
}


class BackendError(Exception):
    """利用者に伝えるエラー。code は CODES のどれか。"""

    def __init__(self, code: str, message: str, detail: str | None = None, params: dict | None = None):
        if code not in CODES:
            code = "INTERNAL"
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.params = params or {}

    @property
    def http_status(self) -> int:
        return CODES[self.code]

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message, "params": self.params}
        if self.detail:
            d["detail"] = self.detail
        return d


class JobCancelled(Exception):
    """ジョブが取り消された。"""


# ---------------------------------------------------------------------------
# 子プロセスの出力の分類（上から順に当てる）
# ---------------------------------------------------------------------------
_PATTERNS = [
    ("GPU_MEMORY_LOW", re.compile(r"CUDA out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED|"
                                  r"cudaErrorMemoryAllocation|out of memory", re.IGNORECASE)),
    ("GPU_NOT_FOUND", re.compile(r"Found no NVIDIA driver|No CUDA GPUs are available|no CUDA-capable device|"
                                 r"CUDA driver version is insufficient|driver on your system is too old|"
                                 r"Torch not compiled with CUDA|cudaErrorNoDevice|nvidia-smi.*not found", re.IGNORECASE)),
    ("LIBRARY_MISSING", re.compile(r"ModuleNotFoundError|ImportError|DLL load failed|"
                                   r"cannot import name|No module named", re.IGNORECASE)),
]


# GPU_NOT_FOUND のうち、ドライバーが古いもの（params.reason = driver_old。画面は Error_GPU_NOT_FOUND_driver_old_*。点検 C32）
DRIVER_OLD_RE = re.compile(r"CUDA driver version is insufficient|driver on your system is too old", re.IGNORECASE)


# 失敗でなくても出る出力（TRELLIS 実行環境では xformers が triton を探して毎回トレースバックを出す。docs/trellis_setup.md 9 章 #6）
_BENIGN = [
    re.compile(r"A matching Triton is not available[^\n]*\n"),
    re.compile(r"Traceback \(most recent call last\):(?:(?!Traceback \().)*?No module named 'triton'[^\n]*\n?", re.DOTALL),
]


_TB = "Traceback (most recent call last)"
# 例外の連鎖（Python が 2 つのトレースバックの間に書く文）。TRELLIS は手元のモデルの読み込みに失敗すると、例外を問わず
# Hugging Face からの取得に切り替える（generate_glb.py はその取得を止める）ので、本当の原因は連鎖の最初の例外にある
_CHAIN_RE = re.compile(r"\n\s*(?:During handling of the above exception, another exception occurred:|"
                       r"The above exception was the direct cause of the following exception:)\s*$")


def _first_chained_exception(t: str, last: int) -> str | None:
    """最後のトレースバック（位置 last）が連鎖の続きなら、連鎖の最初のトレースバックの例外の行（型とメッセージ）。"""
    first = None
    j = last
    while j > 0 and _CHAIN_RE.search(t[:j]):
        k = t.rfind(_TB, 0, j)
        if k < 0:
            break
        first, j = (k, j), k
    if first is None:
        return None
    block = _CHAIN_RE.sub("", t[first[0]:first[1]].rstrip()).rstrip().splitlines()
    exc: list[str] = []
    for ln in reversed(block):            # 末尾の字下げの無い行（File の行・コードの行は字下げされている）
        if not ln.strip() or ln.startswith((" ", "\t")) or ln.startswith(_TB):
            break
        exc.insert(0, ln)
    return "\n".join(exc[-5:]) or None


def failure_text(text: str, lines: int = 60) -> str:
    """出力から、失敗の原因が書かれている部分（最後のトレースバック、無ければ末尾）を取り出す。
    最後のトレースバックが例外の連鎖の続きなら、連鎖の最初の例外の行も先頭に付ける。"""
    t = text or ""
    for rx in _BENIGN:
        t = rx.sub("", t)
    i = t.rfind(_TB)
    part = t[i:] if i >= 0 else t
    out = "\n".join(part.splitlines()[-lines:])
    first = _first_chained_exception(t, i) if i > 0 else None
    if first and first not in out:
        out = f"(first exception of the chain) {first}\n{out}"
    return out


def classify_output(text: str, default: str) -> str:
    t = failure_text(text)
    for code, rx in _PATTERNS:
        if rx.search(t):
            return code
    return default


def tail(text: str, lines: int = 25) -> str:
    return "\n".join((text or "").splitlines()[-lines:])
