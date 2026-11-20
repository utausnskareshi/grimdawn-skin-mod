"""TRELLIS（microsoft/TRELLIS-image-large、microsoft/TRELLIS-text-xlarge。D-003）。

``scripts/trellis/generate_glb.py`` を TRELLIS 実行環境の Python で子プロセスとして動かす（スクリプトは変更しない）。
既定の後処理は ``postprocess_alt``（D-010。``--postprocess`` は付けない）、候補の選別（D-012、``--select-pose``）、
「武器なし」の付加と武器らしい候補の除外（D-013。``--allow-weapons`` を付けない）。テキストは ``--batch`` の JSON で渡し、
プロンプトをコマンドラインに出さない。

背景の自動除去（D-020）: 透明部分の無い画像は、TRELLIS の ``preprocess_image`` が rembg（u2net）で背景を除く。rembg は
``U2NET_HOME`` の下にモデルが無いと GitHub から取得しようとするので、モデルが実行環境にあるときだけ受け付け
（:func:`find_u2net`）、``U2NET_HOME`` をその場所に向けて起動する。無いときは生成の前に止める（pipeline）。
それでも生成の途中で無かったときは、生成スクリプトが取得を止めて（既定。gen_helpers.block_downloads）、
MODEL_MISSING（reason download_blocked）になる。

子プロセスの環境変数は :func:`runtime_env`（実行環境の中の固定した DINOv2・CLIP の場所と注意機構の設定。利用者の
``TORCH_HOME``・``HF_HOME`` などで上書きされないようにする）。生成の前の確認（:meth:`TrellisProvider.check`）は、
モデルのほかに DINOv2（画像）と CLIP（テキスト）がセットアップの固定どおりにあるかも見る。
"""
from __future__ import annotations

import functools
import glob
import hashlib
import json
import os
import re
import threading
import unicodedata

from .. import consent, paths
from ..errors import BackendError
from ..i18n import tr
from ..settings import resolve_trellis_root, trellis_root_candidates
from .base import CheckItem, GenerationOutput, GenerationProvider, GenerationRequest, ProgressUpdate, ProviderRun

MODEL_IDS = {"image": "microsoft/TRELLIS-image-large", "text": "microsoft/TRELLIS-text-xlarge"}
MODEL_DIRS = {"image": "TRELLIS-image-large", "text": "TRELLIS-text-xlarge"}

_STAGE_RE = re.compile(r"^\[stage\]\s+(.+?)\s{2,}\s*[\d.]+s\b")
_TQDM_RE = re.compile(r"Sampling:\s+(\d+)%")
_CAND_RE = re.compile(r"^\[candidate\] seed (\d+): arm (\S+) deg \(([^)]*)\), weapon (\w+)")
_SELECT_RE = re.compile(r"^\[select\] .*?: seed (\d+)")
_GLB_RE = re.compile(r"^\[glb\] (\S+\.glb): (\d+) verts, (\d+) tris")

# 背景の自動除去（D-020）: rembg が探す場所（U2NET_HOME/u2net.onnx と U2NET_HOME/models/u2net/u2net.onnx）を
# 実行環境の中で探す。見つかった場所の「U2NET_HOME」を返す。
U2NET_HOMES = ("u2net", "rembg", "models", ".")
U2NET_MD5 = "60024c5c889badc19c04ad937298a77b"      # rembg 2.0.85 が u2net.onnx の取得時に照合する値
_md5_cache: dict[tuple, str] = {}
_md5_lock = threading.Lock()


def _md5(path: str) -> str:
    st = os.stat(path)
    key = (os.path.normcase(path), st.st_size, int(st.st_mtime))
    with _md5_lock:
        if key in _md5_cache:
            return _md5_cache[key]
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    with _md5_lock:
        _md5_cache[key] = h.hexdigest()
    return _md5_cache[key]


def find_u2net(root: str | None) -> dict | None:
    """実行環境の中の u2net.onnx → {"path", "home"（U2NET_HOME に渡す場所）, "md5_ok"}。無ければ None。"""
    if not root:
        return None
    for sub in U2NET_HOMES:
        home = os.path.normpath(os.path.join(root, sub))
        for p in (os.path.join(home, "u2net.onnx"), os.path.join(home, "models", "u2net", "u2net.onnx")):
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                try:
                    ok = _md5(p) == U2NET_MD5
                except OSError:
                    ok = False
                return {"path": p, "home": home, "md5_ok": ok}
    return None


def u2net_expected(root: str | None) -> str:
    """モデルを置く既定の場所（案内用）。"""
    return os.path.join(root or tr("<TRELLIS 実行環境>", "<TRELLIS runtime>"), "u2net", "u2net.onnx")


# Hugging Face の取得先を HF_HOME とは別の場所に変える変数。利用者の環境にあると、固定した CLIP（<root>\hf-home）の
# 代わりにそちらを読みに行き、HF_HUB_OFFLINE=1 のため（接続の有無に関係なく）テキスト→3D が失敗する
HF_CACHE_OVERRIDES = ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE", "PYTORCH_TRANSFORMERS_CACHE",
                      "PYTORCH_PRETRAINED_BERT_CACHE")


def runtime_env(root: str) -> dict:
    """TRELLIS 実行環境 root で生成・動作確認をするときの環境変数（値が None のものは子プロセスから消す）。

    利用者の TORCH_HOME・HF_HOME・ATTN_BACKEND などが子プロセスにそのまま渡ると、固定した DINOv2（D-025）・CLIP の
    場所や注意機構の設定を上書きするので、実行環境から決まる値を明示する。生成（:meth:`TrellisProvider.prepare`）と
    セットアップの動作確認（setup の runtime_check）で同じ値を使う。

    NVIDIA の GPU が 2 台以上ある PC では、環境チェック・生成の事前確認が判定に使った GPU を CUDA_VISIBLE_DEVICES（UUID）で
    渡す（:func:`envcheck.gpu_env`。点検 C32）。利用者が CUDA_VISIBLE_DEVICES を設定していれば何も足さない（番号の指定も
    CUDA の既定の数え方のまま。判定の側が CUDA に 0 番の GPU を尋ねてそろえる。点検 X3X-01・D2_backend-04）。
    """
    from ..envcheck import gpu_env
    env = {
        "TORCH_HOME": os.path.join(root, "torch-home"),       # 固定した DINOv2 のハブのコードと重み（D-025）
        "HF_HOME": os.path.join(root, "hf-home"),             # 固定した CLIP（テキスト）
        "WARP_CACHE_PATH": os.path.join(root, "warp-cache"),  # kaolin が読み込む NVIDIA warp の作業場所
        "HF_HUB_OFFLINE": "1",                                # 外部へ問い合わせない（generate_glb.py の既定と同じ）
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "ATTN_BACKEND": "xformers",                           # flash-attn は sm_120 の Windows 用が無い
        "SPARSE_ATTN_BACKEND": "xformers",
        "SPARSE_BACKEND": "spconv",
        "SPCONV_ALGO": "native",
        # 背景の除去のモデルの置き場所（D-020。利用者のホームの ~/.u2net は使わない）
        "U2NET_HOME": (find_u2net(root) or {}).get("home") or os.path.join(root, "u2net"),
    }
    env.update({k: None for k in HF_CACHE_OVERRIDES})
    env.update(gpu_env())
    return env


# 生成の前に確かめるセットアップの部品（scripts/trellis/setup_manifest.json）: 画像は DINOv2（固定したハブのコードと重み）、
# テキストは CLIP（refs/main・トークナイザー・重み）。無い・途中・固定と違う版のままだと、TRELLIS が固定していない版を
# 照合なしに取得しに行くか（オンライン）、分かりにくく失敗する（オフライン）ので、生成の前に止めてセットアップへ案内する
MODEL_COMPONENTS = {"image": ("dinov2", "DINOv2"),
                    "text": ("clip", "openai/clip-vit-large-patch14")}
# 部品の表示名に括弧で添える取得元（括弧は表示言語に合わせる。_component_item）
MODEL_SOURCES = {"dinov2": "facebookresearch/dinov2"}


# 生成プロセスの中の進捗（0〜1。ジョブ全体への割り当ては pipeline が決める）
P_IMPORT, P_LOAD, P_CUDA, P_PRE = 0.05, 0.15, 0.20, 0.21
P_CAND_END, P_GLB, P_EXPORT = 0.80, 0.95, 0.97
# 候補 1 体の中の割合: get_cond → 疎構造のサンプリング → SLat のサンプリング → デコード
SUB = {"get_cond": 0.05, "sample_sparse_structure": 0.45, "sample_slat": 0.85, "decode_slat mesh": 0.95,
       "decode_slat gaussian": 1.0}


_NO_OBJECT_RE = re.compile(r"ERROR: image (\d+): no object was found")
# 生成スクリプトの決まった文言（scripts/trellis/gen_helpers.py の *_MARKER と同じ）
_EMPTY_RESULT_RE = re.compile(r"ERROR: no object was generated")
_MODEL_FILES_RE = re.compile(r"ERROR: model files are missing or damaged|DINOv2 hub code is missing")
# 手元のファイルの読み込みに失敗した後の、取得への切り替えを止めた例外。最初の例外（ライブラリの不足など）の分類を優先する
_NOT_LOADED_RE = re.compile(r"model file not loaded: ")
# 背景除去のモデル（u2net.onnx）が壊れている・途中で切れている（TRELLIS の preprocess_image の rembg → onnxruntime の
# 読み込みの失敗。例外は OSError ではない。bg_preview.py の reason u2net_damaged と同じ扱い。点検 R10A-02）
_U2NET_BAD_RE = re.compile(r"\b(?:INVALID_PROTOBUF|NO_SUCHFILE)\b[^\n]*Load model from [^\n]*u2net\.onnx failed")
# PC のメモリ（RAM）・仮想メモリ（ページングファイル）の不足（点検 R10A-03）。GPU メモリの不足（torch.OutOfMemoryError・
# CUDA out of memory）は errors の GPU_MEMORY_LOW が先に当たる（\b により OutOfMemoryError には当たらない）。
# 番号の無い Windows の文（ページングファイル）は表示言語ごとに違うので _paging_file_text で当てる
_SYSTEM_MEMORY_RE = re.compile(r"WinError 1455|paging file is too small|"
                               r"DefaultCPUAllocator: (?:not enough|can't allocate) memory|\b(?:_Array)?MemoryError\b|"
                               r"Unable to allocate [\d.]+ [KMGTPE]?i?B")


@functools.lru_cache(maxsize=1)
def _paging_file_text() -> str | None:
    """Windows のエラー 1455（ページングファイルが小さすぎる）の、この PC の言語の文。DLL の読み込みの失敗の文
    （"DLL load failed while importing _C: <文>"）には番号が無く、文は Windows の表示言語になる（生成の子プロセスと同じ PC）。"""
    try:
        import ctypes
        t = ctypes.FormatError(1455).strip()
    except (ImportError, AttributeError, OSError, ValueError):
        return None
    if t and unicodedata.category(t[-1]).startswith("P"):        # 最後の句点は除く（文の後ろに続きが付くことがある）
        t = t[:-1]
    return t or None


def _system_memory(text: str) -> bool:
    """失敗の本文（failure_text）の例外の行が PC のメモリ不足か。字下げされた行（トレースバックのコードの行。
    ``except MemoryError:`` など）は見ない。"""
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith((" ", "\t"))]
    paging = _paging_file_text()
    return any(_SYSTEM_MEMORY_RE.search(ln) or (paging and paging in ln) for ln in lines)
# 引数の誤り（argparse の usage と error。終了コード 2）。モデルの読み込みの失敗ではない
_ARGPARSE_RE = re.compile(r"^usage: generate_glb\.py|^generate_glb\.py: error: ", re.MULTILINE)
_EMPTY_CAND_RE = re.compile(r"^\[candidate\] seed (\d+): empty result \((\w+)\)")
# 厳格なオフライン（SF17）で止めた接続（gen_helpers.OFFLINE_MARKER と同じ文言。接続先は「名前:ポート」）
_OFFLINE_RE = re.compile(r"\[offline\] blocked network access to (\S+)")
# 止めた接続そのものが失敗の原因（gen_helpers.OfflineBlocked の文言が失敗の本文＝最後の例外にある）
_OFFLINE_FAIL_RE = re.compile(r"strict offline mode: network access to (\S+) is blocked")
# 既定（厳格なオフラインでない）で止めた、生成の途中のモデルの取得（SF17 (2)。gen_helpers.DOWNLOAD_MARKER と
# DOWNLOAD_BLOCKED_TEXT と同じ文言。「ファイル名 from 取得先のホスト」）。ファイルが無いので取得しようとした
_DOWNLOAD_RE = re.compile(r"^\[download\] blocked (\S+) from (\S+)")
_DOWNLOAD_FAIL_RE = re.compile(r"model download during generation is blocked: (\S+) from ([^\s()]+)")

# サンプリングの段数・ガイダンスの強さ（SF05。設定 sampler_preset）→ 生成スクリプトの引数。standard（既定）は何も渡さず、
# モデルの pipeline.json（画像: 両段階 25 段・5.0、テキスト: 25 段・7.5）のまま。fast は 12 段: 画像は TRELLIS の公式の
# デモ（app.py・example_multi_image.py）の値、テキストは公式のデモ（app_text.py）の強さ 7.5 のまま段数だけ 12
SAMPLER_PRESETS = {
    "standard": {},
    "fast": {"image": {"ss_steps": 12, "ss_cfg": 7.5, "slat_steps": 12, "slat_cfg": 3.0},
             "text": {"ss_steps": 12, "ss_cfg": 7.5, "slat_steps": 12, "slat_cfg": 7.5}},
}


# 候補の生成結果（raw sample）の保存（SF14・SF06。設定 keep_candidates）: 生成の出力フォルダーの samples\<名前>_seed<N>.pt
# （generate_glb.py --sample-dir の名前の決まり）。--from-dump で読み直して GLB だけを作る（weights_only=True で読む。C24）
SAMPLE_DIR = "samples"
# 保存した生成結果が読めない（壊れた・途中までのファイル、別の形式）。--from-dump の torch.load(weights_only=True) の失敗
# （読み込みは measure.Stage「load dump」の中で、Stage は例外で抜けたときも「[stage] load dump」の行を出す。点検 X3X-01）
_SAMPLE_BAD_MARKER_RE = re.compile(r"ERROR: the saved sample cannot be read")     # 生成スクリプトが読み込みの失敗を決まった文言で止めたとき
_SAMPLE_BAD_RE = re.compile(r"UnpicklingError|Weights only load failed|PytorchStreamReader|failed finding central directory"
                            r"|invalid load key|EOFError|zipfile|No such file or directory|FileNotFoundError")
# 候補の生成結果を保存できなかった（ディスクの空きなど）。生成は続く（その候補は保存なしの扱い。点検 UA2-R3）
_SAMPLE_FAIL_RE = re.compile(r"^\[sample\] WARNING: seed (\d+) not saved")


def sample_file(out_dir: str, name: str, seed: int) -> str:
    """生成の出力フォルダー out_dir の、シード seed の保存した生成結果のパス（あるとは限らない）。"""
    return os.path.join(out_dir, SAMPLE_DIR, f"{name}_seed{int(seed)}.pt")


def sampler_args(preset: str | None, kind: str, slat_only: bool = False) -> list[str]:
    """設定 sampler_preset → generate_glb.py の --ss-steps などの引数（standard・不明な値は空）。slat_only（見た目の
    作り直し。SF10）は SLat の段数・強さだけ（疎構造はサンプリングしない）。"""
    vals = (SAMPLER_PRESETS.get(preset or "standard") or {}).get(kind) or {}
    out: list[str] = []
    for k in (("slat_steps", "slat_cfg") if slat_only else ("ss_steps", "ss_cfg", "slat_steps", "slat_cfg")):
        if k in vals:
            out += ["--" + k.replace("_", "-"), f"{vals[k]:g}"]
    return out
# 文章の切り詰め。「with the no-weapon suffix」は、利用者の文だけなら収まり、「武器なし」の付け足しの分で超えたとき
_PROMPT_RE = re.compile(r"^\[prompt\] WARNING: (\d+) tokens( with the no-weapon suffix)?; CLIP reads (\d+)")
# 見た目の作り直し（SF10。generate_glb.py --variant-base。variant_base.BAD_BASE_MARKER と同じ文言）
_VARIANT_RE = re.compile(r"^\[variant\] base: (\d+) verts, (\d+) tris, (\d+) voxels")
_VARIANT_BAD_RE = re.compile(r"ERROR: the variant base GLB cannot be used")


class TrellisProvider(GenerationProvider):
    id = "trellis"
    display_name = "TRELLIS"
    inputs = ("image", "text")

    # --- 情報と確認 ----------------------------------------------------------------------------
    def describe(self, settings: dict) -> dict:
        root = resolve_trellis_root(settings)
        checks = {k: [c.to_dict() for c in self.check(settings, k)] for k in self.inputs}
        return {
            "id": self.id, "display_name": self.display_name, "inputs": list(self.inputs),
            "models": {k: {"id": MODEL_IDS[k], "dir": MODEL_DIRS[k]} for k in self.inputs},
            "root": root, "root_candidates": trellis_root_candidates(settings),
            "available": {k: all(c["status"] in ("ok", "warning") for c in checks[k]) for k in self.inputs},
            "checks": checks,
            "settings": ["seed", "select_pose", "candidates", "allow_weapons", "simplify", "texture_size",
                         "sampler_preset", "low_vram", "strict_offline", "keep_candidates"],
            "rebuild_from_sample": True,       # 保存した生成結果から GLB を作り直せる（SF14。prepare_rebuild）
            "sampler_presets": SAMPLER_PRESETS,
            "postprocess": "postprocess_alt (D-010)",
            "background_removal": self.background_removal(settings),
            "max_images": self.MAX_IMAGES, "multi_image_supported": self.multi_image_supported(),
            "multi_modes": ["stochastic", "multidiffusion"],
            # 装備の生成結果を元に、文章で見た目だけを作り直せる（SF10。テキストの任意部品と CLIP の同意が要る）
            "text_variant": self.variant_supported(),
        }

    MAX_IMAGES = 4

    def multi_image_supported(self) -> bool:
        """生成スクリプトが複数画像（--image を複数回、--multi-mode）に対応しているか（スクリプトの引数の定義を見る）。"""
        try:
            with open(os.path.join(paths.TRELLIS_SCRIPTS, "generate_glb.py"), encoding="utf-8") as f:
                return '"--multi-mode"' in f.read()
        except OSError:
            return False

    def variant_supported(self) -> bool:
        """生成スクリプトが見た目の作り直し（--variant-base。SF10）に対応しているか。"""
        try:
            with open(os.path.join(paths.TRELLIS_SCRIPTS, "generate_glb.py"), encoding="utf-8") as f:
                return '"--variant-base"' in f.read()
        except OSError:
            return False

    def background_removal(self, settings: dict) -> dict:
        root = resolve_trellis_root(settings)
        f = find_u2net(root)
        return {"available": f is not None, "path": f["path"] if f else None, "md5_ok": f["md5_ok"] if f else None,
                "expected": u2net_expected(root), "model": "u2net (rembg)"}

    def check(self, settings: dict, kind: str | None = None) -> list[CheckItem]:
        root = resolve_trellis_root(settings)
        items: list[CheckItem] = []
        if root is None:
            items.append(CheckItem("trellis_runtime", "missing", "LIBRARY_MISSING",
                                   tr("TRELLIS 実行環境（venv\\Scripts\\python.exe）が見つかりません",
                                      "The TRELLIS runtime (venv\\Scripts\\python.exe) was not found"),
                                   {"candidates": trellis_root_candidates(settings)}))
            return items
        items.append(CheckItem("trellis_runtime", "ok", None, root, {"root": root}))
        if not os.path.isdir(os.path.join(root, "src", "TRELLIS", "trellis")):
            items.append(CheckItem("trellis_source", "missing", "LIBRARY_MISSING",
                                   tr("TRELLIS のソース（src\\TRELLIS）がありません", "The TRELLIS source (src\\TRELLIS) is missing"),
                                   {"root": root}))
        script = os.path.join(paths.TRELLIS_SCRIPTS, "generate_glb.py")
        if not os.path.isfile(script):
            items.append(CheckItem("trellis_script", "missing", "LIBRARY_MISSING",
                                   tr(f"生成スクリプトがありません: {script}", f"The generation script was not found: {script}")))
        kinds = [kind] if kind else list(self.inputs)
        for k in kinds:
            mdir = os.path.join(root, "models", MODEL_DIRS[k])
            ok = os.path.isfile(os.path.join(mdir, "pipeline.json")) and os.path.isdir(os.path.join(mdir, "ckpts"))
            items.append(CheckItem(f"model_{k}", "ok" if ok else "missing", None if ok else "MODEL_MISSING",
                                   tr(f"{MODEL_IDS[k]}: あり", f"{MODEL_IDS[k]}: installed") if ok
                                   else tr(f"{MODEL_IDS[k]}: ありません", f"{MODEL_IDS[k]}: not installed"),
                                   {"model": MODEL_IDS[k], "dir": mdir}))
            items.append(self._component_item(root, k))      # 画像は DINOv2、テキストは CLIP
            if k == "image":
                f = find_u2net(root)
                if f is None:
                    items.append(CheckItem("bg_removal", "warning", None,
                                           tr("背景除去のモデル（u2net）: ありません（背景が透明な画像だけ使えます）",
                                              "Background removal model (u2net): not installed (only images with a transparent "
                                              "background can be used)"),
                                           {"expected": u2net_expected(root)}))
                else:
                    items.append(CheckItem("bg_removal", "ok" if f["md5_ok"] else "warning", None,
                                           tr("背景除去のモデル（u2net）: あり", "Background removal model (u2net): installed")
                                           + ("" if f["md5_ok"] else tr("（ファイルの照合値が rembg の記録と違います）",
                                                                        " (the file's hash does not match rembg's record)")),
                                           {"path": f["path"], "md5_ok": f["md5_ok"]}))
            if k == "text":
                st = consent.status(root)
                items.append(CheckItem("clip_consent", "ok" if st["accepted"] else "error",
                                       None if st["accepted"] else "CONSENT_REQUIRED",
                                       tr("CLIP の注意事項に同意済み", "The CLIP notice has been accepted") if st["accepted"]
                                       else tr("CLIP の注意事項への同意が必要です",
                                               "Acceptance of the CLIP notice is required"),
                                       {"accepted_at": st["accepted_at"]}))
        return items

    def component_state(self, root: str, cid: str) -> dict:
        """セットアップの部品の状態（setup.component_status。ファイルの有無と大きさ・固定した版の目印だけを見る。速い）。
        DINOv2・CLIP の手順は導入済みのパッケージを使わないので、venv の走査はしない（dists に {} を渡す）。"""
        from .. import setup as gsetup
        return gsetup.component_status(gsetup.component(cid), root, {})

    def _component_item(self, root: str, kind: str) -> CheckItem:
        cid, name = MODEL_COMPONENTS[kind]
        src = MODEL_SOURCES.get(cid)
        label = tr(f"{name}（{src}）", f"{name} ({src})") if src else name
        iid = f"model_{cid}"
        try:
            st = self.component_state(root, cid)
        except (OSError, ValueError, KeyError, TypeError, BackendError) as e:   # セットアップの定義が読めない（配布物の破損など）
            return CheckItem(iid, "warning", None, tr(f"{label}: 確認できません（{type(e).__name__}）",
                                                      f"{label}: cannot be checked ({type(e).__name__})"), {"component": cid})
        state, detail = st.get("state"), st.get("detail") or ""
        params = {"component": cid, "model": label, "state": state, "detail": detail}
        if state == "installed":
            return CheckItem(iid, "ok", None, tr(f"{label}: あり", f"{label}: installed"), params)
        if state == "outdated":
            # 固定の前の版（D-025 の観点で、非商用の文書を含むコードがありうる）。セットアップが取り直す
            return CheckItem(iid, "error", "MODEL_MISSING", tr(f"{label}: 固定した版と違います（{detail}）",
                                                               f"{label}: differs from the pinned version ({detail})"),
                             {**params, "reason": "outdated"})
        return CheckItem(iid, "missing", "MODEL_MISSING",
                         tr(f"{label}: ありません", f"{label}: not installed")
                         + (tr(f"（{detail}）", f" ({detail})") if detail and state == "partial" else ""), params)

    # --- 実行 ----------------------------------------------------------------------------------
    def prepare(self, settings: dict, request: GenerationRequest, out_dir: str) -> ProviderRun:
        root = resolve_trellis_root(settings)
        s = request.settings
        py = os.path.join(root, "venv", "Scripts", "python.exe")
        # -I は PYTHONIOENCODING などを無視するので、-X utf8 で引数の解析より前の出力（日本語の OS のエラー文など）も
        # UTF-8 にする（点検 C41）
        cmd = [py, "-I", "-X", "utf8", os.path.join(paths.TRELLIS_SCRIPTS, "generate_glb.py"), "--out", out_dir,
               "--seed", str(s["seed"]), "--variants", f"{s['simplify']:g}:{s['texture_size']}"]
        if request.kind == "image":
            images = request.images
            for p in images:
                cmd += ["--image", p]
            cmd += ["--name", request.name]
            if len(images) > 1:
                # 複数画像（TRELLIS の run_multi_image。学習なしの手法でカメラの向きは使わない）。1 枚のときは従来どおり何も足さない
                for lab in (request.image_labels or [])[:len(images)]:
                    cmd.append(f"--image-label={lab}")      # 「=」の形: 「-」で始まる名前（-Z 側など）も値として渡る
                cmd += ["--multi-mode", s["multi_mode"]]
        elif request.kind == "text":
            batch = os.path.join(out_dir, "request.json")
            os.makedirs(out_dir, exist_ok=True)
            with open(batch, "w", encoding="utf-8", newline="\n") as f:
                json.dump([{"name": request.name, "prompt": request.text, "seed": s["seed"]}], f, ensure_ascii=False)
            cmd += ["--batch", batch]
        else:
            raise ValueError(tr(f"TRELLIS の入力ではありません: {request.kind}", f"Not an input kind for TRELLIS: {request.kind}"))
        # 見た目の作り直し（SF10）: 元の GLB のボクセルを使い、疎構造はサンプリングしない（候補の選別・候補の保存もしない）
        variant = bool(request.variant_base)
        if variant:
            if request.kind != "text":
                raise ValueError(tr("見た目の作り直しはテキストの入力だけです", "Appearance remakes only accept text input"))
            cmd += ["--variant-base", request.variant_base]
        candidates = 1
        if s["select_pose"] and not variant:
            cmd += ["--select-pose", "--candidates", str(s["candidates"])]
            candidates = s["candidates"]
        if s["allow_weapons"]:
            cmd += ["--allow-weapons"]
        # 詳細設定（既定はどれも何も足さない＝従来と同じコマンド）: サンプリング（SF05）、低 VRAM（SF15）、厳格なオフライン（SF17）
        cmd += sampler_args(s.get("sampler_preset"), request.kind, slat_only=variant)
        if s.get("low_vram"):
            cmd += ["--low-vram"]
        if s.get("strict_offline"):
            cmd += ["--strict-offline"]
        if s.get("keep_candidates") and not variant:     # SF14・SF06: 候補の生成結果を残す（既定は何も足さない）
            cmd += ["--sample-dir", os.path.join(out_dir, SAMPLE_DIR)]
        env = runtime_env(root)
        state = {"candidates": candidates, "cand_index": 0, "sub": 0.0, "last_stage": None, "seen_load": False,
                 "loaded": False, "glb": None, "chosen_seed": None, "kind": request.kind,
                 "images": len(request.images) if request.kind == "image" else 0}
        if variant:
            state["variant"] = True
        return ProviderRun(cmd=cmd, env=env, cwd=root, state=state)

    def prepare_rebuild(self, settings: dict, request: GenerationRequest, sample: str, out_dir: str) -> ProviderRun:
        """保存した生成結果（sample。generate_glb.py --sample-dir の .pt）から GLB だけを作る（--from-dump。SF14・SF06）。

        3D 生成のモデルは読み込まない（後処理は既定の postprocess_alt。D-010）。細かさ・テクスチャの大きさは
        request.settings の simplify・texture_size。厳格なオフライン（SF17）は生成と同じく渡す。サンプリング・低 VRAM・
        候補の設定は生成の段階のものなので渡さない。出力の名前は request.name（collect と同じ）。"""
        root = resolve_trellis_root(settings)
        s = request.settings
        py = os.path.join(root, "venv", "Scripts", "python.exe")
        cmd = [py, "-I", "-X", "utf8", os.path.join(paths.TRELLIS_SCRIPTS, "generate_glb.py"), "--from-dump", sample,
               "--out", out_dir, "--name", request.name, "--variants", f"{s['simplify']:g}:{s['texture_size']}"]
        if s.get("strict_offline"):
            cmd += ["--strict-offline"]
        state = {"candidates": 1, "cand_index": 0, "sub": 0.0, "last_stage": None, "seen_load": False, "loaded": False,
                 "glb": None, "chosen_seed": None, "kind": request.kind, "images": 0, "from_sample": True}
        return ProviderRun(cmd=cmd, env=runtime_env(root), cwd=root, state=state)

    def _cand_progress(self, st: dict) -> float:
        k = max(1, st["candidates"])
        done = min(st["cand_index"], k)           # 作り直しの回（--max-rounds）で k を超えたら末尾に留める
        frac = (done + (st["sub"] if done < k else 0.0)) / k
        return P_PRE + (P_CAND_END - P_PRE) * min(1.0, frac)

    def parse_line(self, line: str, run: ProviderRun) -> ProgressUpdate | None:
        st = run.state
        line = line.strip()
        m = _STAGE_RE.match(line)
        if m:
            name = m.group(1).strip()
            if st.get("from_sample") and st["last_stage"] == "load dump":
                # 「load dump」の次の段階の行で、保存した生成結果を読めたと確定する（「load dump」の行は読み込みが例外で
                # 終わっても出る。点検 X3X-01）
                st["sample_read"] = True
            st["last_stage"] = name
            if name == "import trellis":
                return ProgressUpdate(P_IMPORT, "generate_load")
            if name.startswith("load pipeline"):
                st["seen_load"] = True
                return ProgressUpdate(P_LOAD, "generate_load")
            if name == "pipeline.cuda()":
                st["loaded"] = True
                return ProgressUpdate(P_CUDA, "generate_load")
            if name == "load dump":                 # 保存した生成結果の読み込みの段階が終わった（--from-dump。SF14・SF06。成否は問わない）
                st["loaded"] = True
                return ProgressUpdate(P_CUDA, "generate_load")
            if name == "preprocess_image":
                return ProgressUpdate(P_PRE, "generate_candidates", self._detail(st))
            if name.startswith("get_cond"):
                st["sub"] = SUB["get_cond"]
            elif name in SUB:
                st["sub"] = SUB[name]
                if name == "decode_slat gaussian":
                    st["cand_index"] += 1
                    st["sub"] = 0.0
            elif name.startswith("pose_check"):
                return None
            elif name.startswith("to_glb"):
                return ProgressUpdate(P_GLB, "generate_glb")
            elif name.startswith("export"):
                return ProgressUpdate(P_EXPORT, "generate_glb")
            else:
                return None
            return ProgressUpdate(self._cand_progress(st), "generate_candidates", self._detail(st))
        m = _TQDM_RE.search(line)
        if m and st["loaded"]:
            pct = int(m.group(1)) / 100.0
            last = st["last_stage"] or ""
            if last.startswith("get_cond") and st.get("variant"):    # 見た目の作り直し（SF10）: 疎構造は無く SLat のサンプリング中
                st["sub"] = SUB["get_cond"] + (SUB["sample_slat"] - SUB["get_cond"]) * pct
            elif last.startswith("get_cond"):         # 疎構造のサンプリング中
                st["sub"] = SUB["get_cond"] + (SUB["sample_sparse_structure"] - SUB["get_cond"]) * pct
            elif last == "sample_sparse_structure":  # SLat のサンプリング中
                st["sub"] = SUB["sample_sparse_structure"] + (SUB["sample_slat"] - SUB["sample_sparse_structure"]) * pct
            else:
                return None
            return ProgressUpdate(self._cand_progress(st), "generate_candidates", self._detail(st))
        m = _EMPTY_CAND_RE.match(line)
        if m:
            if m.group(2) == "no_sparse_structure":     # デコードの段階が無いので、ここで次の候補に進める
                st["cand_index"] += 1
                st["sub"] = 0.0
            return ProgressUpdate(None, None, None, log=tr(f"シード {m.group(1)} の候補: 生成結果が空でした（除外）",
                                                           f"Candidate seed {m.group(1)}: the generation result was empty "
                                                           "(excluded)"))
        m = _CAND_RE.match(line)
        if m:
            return ProgressUpdate(None, None, None, log=tr(f"シード {m.group(1)} の候補: 腕の角度 {m.group(2)}°（{m.group(3)}）、"
                                                           f"武器の疑い {m.group(4)}",
                                                           f"Candidate seed {m.group(1)}: arm angle {m.group(2)}° ({m.group(3)}), "
                                                           f"weapon suspected {m.group(4)}"))
        m = _PROMPT_RE.match(line)
        if m:
            if m.group(2):
                return ProgressUpdate(None, None, None, log=tr(
                    f"「武器なし」の指定を足すと {m.group(1)} トークンになり、生成 AI が"
                    f"読む {m.group(3)} トークンを超えたため、文章の後ろの部分は使われません",
                    f"With the \"no weapons\" instruction added, the text is {m.group(1)} tokens, more than the "
                    f"{m.group(3)} tokens the generation AI reads, so the end of the text is not used"))
            return ProgressUpdate(None, None, None, log=tr(f"文章が {m.group(1)} トークンあり、生成 AI が読む {m.group(3)} "
                                                           "トークンを超えたため、後ろの部分は使われません",
                                                           f"The text is {m.group(1)} tokens, more than the {m.group(3)} tokens "
                                                           "the generation AI reads, so the end is not used"))
        m = _OFFLINE_RE.match(line)
        if m:
            return ProgressUpdate(None, None, None, log=tr(f"厳格なオフライン: 生成の途中の外部への接続（{m.group(1)}）を止めました",
                                                           f"Strict offline mode: blocked an outgoing connection during "
                                                           f"generation ({m.group(1)})"))
        m = _DOWNLOAD_RE.match(line)
        if m:
            return ProgressUpdate(None, None, None, log=tr(f"生成の途中のファイルの取得（{m.group(1)}、{m.group(2)}）を止めました"
                                                           "（生成中は取得しません。セットアップで取得してください）",
                                                           f"Blocked a file download during generation ({m.group(1)}, "
                                                           f"{m.group(2)}) (files are not downloaded during generation; "
                                                           "download them in Setup)"))
        m = _VARIANT_RE.match(line)
        if m:
            return ProgressUpdate(None, None, None, log=tr(f"元の形: {m.group(1)} 頂点・{m.group(2)} 三角形 → {m.group(3)} ボクセル"
                                                           "（この形のまま、文章で見た目を作り直します）",
                                                           f"Original shape: {m.group(1)} vertices, {m.group(2)} triangles → "
                                                           f"{m.group(3)} voxels (the appearance is remade from the text, keeping "
                                                           "this shape)"))
        m = _SAMPLE_FAIL_RE.match(line)
        if m:
            return ProgressUpdate(None, None, None, log=tr(f"シード {m.group(1)} の候補: 生成結果を保存できませんでした（ディスクの空き"
                                                           "など）。生成は続けます（この候補は保存なしの扱いです）",
                                                           f"Candidate seed {m.group(1)}: the generation result could not be "
                                                           "saved (not enough disk space, etc.). Generation continues (this "
                                                           "candidate is treated as not saved)"))
        m = _SELECT_RE.match(line)
        if m:
            st["chosen_seed"] = int(m.group(1))
            return ProgressUpdate(P_CAND_END, "generate_glb", {"seed": st["chosen_seed"]},
                                  log=tr(f"候補からシード {m.group(1)} を選びました", f"Chose seed {m.group(1)} from the candidates")
                                  + (tr("（警告あり）", " (with warnings)") if "WARNING" in line else ""))
        m = _GLB_RE.match(line)
        if m:
            st["glb"] = m.group(1)
            return ProgressUpdate(P_EXPORT, "generate_glb", None, log=tr(f"GLB: {m.group(2)} 頂点、{m.group(3)} 三角形",
                                                                         f"GLB: {m.group(2)} vertices, {m.group(3)} triangles"))
        return None

    def _detail(self, st: dict) -> dict:
        return {"index": min(st["cand_index"] + 1, max(1, st["candidates"])), "count": st["candidates"]}

    def classify_failure(self, output: str, run: ProviderRun) -> str:
        from ..errors import DRIVER_OLD_RE, classify_output, failure_text
        m = _NO_OBJECT_RE.search(output or "")
        if m:                                   # 前処理で物体が見つからない画像（背景の除去の後に何も残らない）。終了コード 1
            run.state["error_params"] = {"reason": "image_no_object", "index": int(m.group(1))}
            return "INVALID_INPUT"
        if _VARIANT_BAD_RE.search(output or ""):    # 見た目の作り直しの元の GLB が読めない・三角形が無い（SF10）
            run.state["error_params"] = {"reason": "variant_base_unreadable"}
            return "INVALID_MODEL"
        if run.state.get("from_sample") and _SAMPLE_BAD_MARKER_RE.search(output or ""):    # 保存した生成結果が読めない（SF14）
            run.state["error_params"] = {"reason": "sample_unreadable"}
            return "INVALID_MODEL"
        if _EMPTY_RESULT_RE.search(output or ""):   # 生成結果が空（物体の無い入力画像とは別。シードを変えると出ることがある）
            run.state["error_params"] = {"reason": "empty_result"}
            return "GENERATION_FAILED"
        if _MODEL_FILES_RE.search(output or ""):    # モデルのファイルが無い・壊れている（取得には行かない）
            run.state["error_params"] = {"reason": "model_files"}
            return "MODEL_MISSING"
        if not run.state.get("loaded") and _ARGPARSE_RE.search(output or ""):
            run.state["error_params"] = {"reason": "generator_arguments"}
            return "INTERNAL"
        if _U2NET_BAD_RE.search(failure_text(output or "")):     # 壊れた u2net.onnx（背景除去。点検 R10A-02）
            run.state["error_params"] = {"reason": "u2net_damaged"}
            return "MODEL_MISSING"
        code = classify_output(output, "")
        if code in ("", "LIBRARY_MISSING") and _system_memory(failure_text(output or "")):
            # PC のメモリ・ページングファイルの不足（点検 R10A-03）。DLL の読み込みの失敗（LIBRARY_MISSING）、重みの読み込みの
            # 失敗の連鎖（下の model file not loaded → MODEL_MISSING）、段階による既定の分類より先に当て、ファイルの破損・取り直しへ
            # 案内しない（GPU メモリの不足 GPU_MEMORY_LOW は今のまま優先）
            run.state["error_params"] = {"reason": "system_memory"}
            return "GENERATION_FAILED" if run.state.get("loaded") else "MODEL_LOAD_FAILED"
        if code:
            if code == "GPU_NOT_FOUND" and DRIVER_OLD_RE.search(failure_text(output or "")):
                run.state["error_params"] = {"reason": "driver_old"}          # ドライバーの更新を案内する（点検 C32）
            return code
        cause = _OFFLINE_FAIL_RE.findall(failure_text(output or ""))
        if cause:      # 厳格なオフライン（SF17）が止めた取得で失敗した（無い・壊れたファイルを取りに行こうとした）
            # 止めた行があっても失敗の本文に OfflineBlocked が無ければ（ライブラリが拒否を握りつぶして後で別の理由で
            # 落ちた）、ここでは分類せず従来の分類に進む（止めた接続はログに出ている）
            hosts = sorted(set(_OFFLINE_RE.findall(output or "")) or set(cause))
            run.state["error_params"] = {"reason": "offline_blocked", "hosts": ", ".join(hosts)}
            return "MODEL_MISSING"
        blocked = _DOWNLOAD_FAIL_RE.findall(failure_text(output or ""))
        if blocked:    # 既定で、無いファイル（u2net.onnx・DINOv2 の重み）の生成の途中の取得を止めた（SF17 (2)。D0）
            # 止めた行だけがあって別の例外で落ちたときは、上の厳格なオフラインと同じく従来の分類に進む
            run.state["error_params"] = {"reason": "download_blocked", "file": ", ".join(sorted({f for f, _ in blocked})),
                                         "hosts": ", ".join(sorted({h for _, h in blocked}))}
            return "MODEL_MISSING"
        if _NOT_LOADED_RE.search(output or ""):     # 壊れた・形の合わない重み（セットアップが照合して取り直す）
            run.state["error_params"] = {"reason": "model_files"}
            return "MODEL_MISSING"
        if run.state.get("from_sample") and not run.state.get("sample_read") and _SAMPLE_BAD_RE.search(failure_text(output or "")):
            # 保存した生成結果（下書きの samples の .pt）が読めない。モデルの読み込みの失敗ではない（SF14・SF06）。
            # 「load dump」の行の後でも、次の段階の行が出る前の失敗はここ（点検 X3X-01）
            run.state["error_params"] = {"reason": "sample_unreadable"}
            return "INVALID_MODEL"
        if not run.state.get("loaded"):
            return "MODEL_LOAD_FAILED"
        return "GENERATION_FAILED"

    def collect(self, settings: dict, request: GenerationRequest, out_dir: str, run: ProviderRun) -> GenerationOutput:
        s = request.settings
        tag = f"s{s['simplify']:g}_t{s['texture_size']}"
        glb = os.path.join(out_dir, f"{request.name}_{tag}.glb")
        if not os.path.isfile(glb):
            raise FileNotFoundError(glb)
        files = {}
        for role, suffix in (("metrics", "_metrics.json"), ("selection", "_selection.json"),
                             ("candidates_png", "_candidates.png"), ("preprocessed_png", "_preprocessed.png"),
                             ("views_png", f"_{tag}_views.png")):
            p = os.path.join(out_dir, request.name + suffix)
            if os.path.isfile(p):
                files[role] = p
        pre = sorted(glob.glob(os.path.join(out_dir, request.name + "_preprocessed*.png")))
        if pre:
            info_pre = pre
            files.setdefault("preprocessed_png", pre[0])
        else:
            info_pre = []
        seed, warnings, info = s["seed"], [], {"preprocessed": info_pre}
        if "metrics" in files:
            with open(files["metrics"], encoding="utf-8") as f:
                met = json.load(f)
            sel = met.get("selection") or {}
            if sel.get("chosen_seed") is not None:
                seed = int(sel["chosen_seed"])
            elif met.get("runs"):
                seed = int(met["runs"][0].get("seed", seed))
            # 作り直しで試していないシードの最初（選別が 2 回目まで進むと、要求のシード + 2×候補数 まで使っている）
            if sel.get("rounds_used") and sel.get("candidates_per_round"):
                info["next_seed"] = int(s["seed"]) + int(sel["rounds_used"]) * int(sel["candidates_per_round"])
            else:
                info["next_seed"] = seed + 1
            if sel.get("warning"):
                warnings.append("selection: " + sel["warning"])
            pc = met.get("pose_check") or {}
            info["pose"] = {k: pc.get(k) for k in ("arm_angle_deg", "arm_angle_error_deg", "pose_class",
                                                   "weapon_suspected", "weapon_reasons")}
            if pc.get("weapon_suspected"):
                warnings.append("weapon_suspected")
            # 腕・武器の判定は正面 +Z を仮定している。180° 回すと判定が変わる体（翼・尻尾など。B-004）を知らせる
            od = met.get("pose_orientation_differs")
            if od:
                info["pose"]["orientation_differs"] = od
                warnings.append("pose_orientation_sensitive")
            v = (met.get("variants") or [{}])[0]
            info["glb"] = {k: v.get(k) for k in ("vertices", "triangles", "extent")}
            info["prompt_used"] = (met.get("info") or {}).get("prompt_used")
            mi = met.get("info") or {}
            # テキスト: CLIP の 77 トークンを超えて、利用者の文の後ろが使われなかった（D-013 の「武器なし」は残す）
            for k in ("prompt_tokens", "prompt_truncated", "no_weapon_kept"):
                if k in mi:
                    info[k] = mi[k]
            # 詳細設定（生成側は使ったときだけ記録する。SF05 サンプリング・SF15 低 VRAM・SF17 厳格なオフライン）
            # 見た目の作り直し（SF10）の元の記録（variant。使ったときだけ）
            for k in ("sampler_overrides", "sampler_params", "low_vram", "strict_offline", "variant"):
                if k in mi:
                    info[k] = mi[k]
            if "offline_blocked" in met:
                info["offline_blocked"] = met["offline_blocked"]
            if met.get("saved_samples"):        # 候補の生成結果の保存（SF14・SF06。設定 keep_candidates のときだけ）
                info["saved_samples"] = [{k: e.get(k) for k in ("seed", "file", "bytes")} for e in met["saved_samples"]
                                         if isinstance(e, dict)]
            if met.get("sample_save_failed"):   # 保存できなかった候補（生成は成功。点検 UA2-R3）→ 生成の注意
                info["sample_save_failed"] = [e.get("seed") for e in met["sample_save_failed"] if isinstance(e, dict)]
                warnings.append("sample_save_failed")
            if mi.get("prompt_truncated"):
                # 「武器なし」の指定が CLIP の読む範囲に残ったか。武器を許可すると付け足さないので、注意の文を分ける
                # （no_weapon_kept の無い古い報告は設定から）
                kept = mi.get("no_weapon_kept")
                if kept is None:
                    kept = not s.get("allow_weapons")
                warnings.append("prompt_truncated" if kept else "prompt_truncated_weapons_allowed")
            for k in ("input_images", "multi_mode"):          # 複数画像の報告（生成側が記録する）
                if met.get(k) is not None or mi.get(k) is not None:
                    info[k] = met.get(k) if met.get(k) is not None else mi.get(k)
            # 前処理後の画像は報告の順（index）に並べる。報告に無い・ファイルが欠けていればファイル名の順のまま
            ii = info.get("input_images")
            entries = sorted(ii.get("images") or [], key=lambda e: e.get("index", 0)) if isinstance(ii, dict) else []
            listed = [os.path.join(out_dir, e["preprocessed"]) for e in entries if e.get("preprocessed")]
            if listed and all(os.path.isfile(p) for p in listed):
                info["preprocessed"] = listed
                files["preprocessed_png"] = listed[0]
        return GenerationOutput(glb=glb, model_id=MODEL_IDS[request.kind], seed=seed, files=files, warnings=warnings,
                                axes=None, info=info)
