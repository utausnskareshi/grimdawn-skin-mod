"""バックエンドの設定（``<データ>\\app\\backend_settings.json``。docs/app_architecture.md 6.2 節）。

画面（WPF）は ``GET /settings`` で読み、``PUT /settings`` で変更する項目だけを送る。値の検査はここで行い、
不正な値は :class:`~gdskin_backend.errors.BackendError`（INVALID_INPUT）にする。
"""
from __future__ import annotations

import copy
import glob
import json
import os
import pathlib
import shutil
import threading
import time

from . import logs, paths
from .errors import BackendError
from .i18n import tr

GENERATION_DEFAULTS = {
    "provider": "trellis",
    "seed": 1,
    "select_pose": True,       # D-012
    "candidates": 4,
    "allow_weapons": False,    # D-013
    "simplify": 0.9,           # 約 23,000 頂点（D-009 の目標に近い）
    "texture_size": 1024,
    "target_vertices": 25000,  # 変換の目標頂点数（D-009）
    "multi_mode": "stochastic",  # 複数画像のとき（TRELLIS の run_multi_image。既定は生成側の評価の後で決める）
    # --- 詳細設定（どれも既定では今までと同じ動き。2026-10-17 追加機能 U-A1） ---
    # SF05: サンプリングの段数・ガイダンスの強さ。standard = モデルの pipeline.json のまま（生成スクリプトに何も渡さない）、
    # fast = 12 段（値は providers.trellis.SAMPLER_PRESETS）
    "sampler_preset": "standard",
    "low_vram": False,             # SF15: デコードの間、条件付け・流れのモデルを CPU に逃がす（結果は同じ、時間が少し増える）
    "strict_offline": False,       # SF17: 生成の子プロセスの外部への接続をすべて拒否し、試みた接続先を記録する
    "convert_texture_max": 2048,   # SF24: 変換のテクスチャの最大辺（gdconvert の texture_max_size。2048 は今までと同じ）
    # SF14・SF06（U-A2）: 候補の生成結果（raw sample。1 体約 50 MB）を下書きの generate\samples に残し、選んだ候補・
    # GLB の作り直しを 3D 生成なしで行えるようにする。下書きにだけ置く（装備・.gdskin には入れない。下書きと一緒に消える）
    "keep_candidates": False,
    # SF20（U-B）: 背景除去を生成の前にアプリ側（TRELLIS 実行環境の Python で bg_preview.py remove）で行い、透明な PNG を
    # 渡す。オフ（既定）は今までどおり TRELLIS の中で除く。後処理（rembg.remove の引数）はアプリ側のときだけ使う
    "bg_app_removal": False,
    "bg_post_process_mask": False,  # マスクを滑らかな 0/255 にする（モルフォロジーとぼかし）
    "bg_alpha_matting": False,      # pymatting の closed-form のアルファマッティング（CPU で数秒〜数十秒）
    "bg_decontaminate": False,      # 縁に残る背景の色を除く（alpha_matting のときは使われない。rembg の仕様）
}

BG_OPTION_KEYS = ("bg_post_process_mask", "bg_alpha_matting", "bg_decontaminate")
MULTI_MODES = ("stochastic", "multidiffusion")
SAMPLER_PRESETS = ("standard", "fast")
CONVERT_TEXTURE_MAX = (512, 1024, 2048)

SETTINGS_VERSION = 2

DEFAULTS = {
    "version": SETTINGS_VERSION,
    "game_dir": "",                # 空 = Steam から自動検出
    "install_target_dir": "",      # 空 = game_dir（テスト用に別のフォルダーを導入先に見立てるときだけ指定）
    "trellis_root": "",            # 空 = 自動（trellis_root_candidates: <データ>\trellis-runtime → セットアップの既定の取得先 → 開発時はリポジトリの隣）
    "keep_input_image": True,
    "keep_drafts": False,
    # Text_JA.arc に日本語名を入れるか（"JA" を含めば入れる）。Text_XX.arc は指定に依らず常に 13 言語分を作る
    # （2026-10-17 利用者の判断 C124。既定は日本語名を入れる。設定の版 1 の値 ["EN"] は既定に戻す）
    "build_languages": ["EN", "JA"],
    "include_dev_equipment": False,
    "generation": GENERATION_DEFAULTS,
}

_PATH_KEYS = ("game_dir", "install_target_dir", "trellis_root")
_BOOL_KEYS = ("keep_input_image", "keep_drafts", "include_dev_equipment")


def settings_path() -> str:
    return os.path.join(paths.app_dir(), "backend_settings.json")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def validate_generation(g: dict) -> dict:
    """生成設定を検査して正規化する（画面からの要求ごとの上書きにも使う）。"""
    if g is not None and not isinstance(g, dict):
        # 配列・文字列は .items() の例外で INTERNAL・起動の失敗になっていた（点検 R09A-02）
        raise BackendError("INVALID_INPUT", tr("生成設定（generation）は JSON のオブジェクトで指定してください",
                                               "The generation settings (generation) must be a JSON object"))
    out = dict(GENERATION_DEFAULTS)
    out.update({k: v for k, v in (g or {}).items() if k in GENERATION_DEFAULTS})
    try:
        out["seed"] = int(out["seed"])
        out["candidates"] = int(out["candidates"])
        out["simplify"] = float(out["simplify"])
        out["texture_size"] = int(out["texture_size"])
        out["target_vertices"] = int(out["target_vertices"])
        out["convert_texture_max"] = int(out["convert_texture_max"])
    except (TypeError, ValueError, OverflowError) as e:     # OverflowError: JSON の Infinity（R09A-02）
        raise BackendError("INVALID_INPUT", tr(f"生成設定の値が数値ではありません: {e}",
                                               f"A generation setting value is not a number: {e}"))
    out["select_pose"] = bool(out["select_pose"])
    out["allow_weapons"] = bool(out["allow_weapons"])
    out["low_vram"] = bool(out["low_vram"])
    out["strict_offline"] = bool(out["strict_offline"])
    out["keep_candidates"] = bool(out["keep_candidates"])
    for k in ("bg_app_removal",) + BG_OPTION_KEYS:     # SF20（U-B）
        out[k] = bool(out[k])
    problems = []
    if not 0 <= out["seed"] <= 2**31 - 1:
        problems.append(tr("seed は 0〜2147483647", "seed must be 0 to 2147483647"))
    if not 1 <= out["candidates"] <= 8:
        problems.append(tr("candidates は 1〜8", "candidates must be 1 to 8"))
    if not 0.5 <= out["simplify"] <= 0.99:
        problems.append(tr("simplify は 0.5〜0.99", "simplify must be 0.5 to 0.99"))
    if out["texture_size"] not in (512, 1024, 2048):
        problems.append(tr("texture_size は 512 / 1024 / 2048", "texture_size must be 512, 1024 or 2048"))
    if not 2000 <= out["target_vertices"] <= 65535:
        problems.append(tr("target_vertices は 2000〜65535（65,535 はゲームの頂点数の絶対上限）",
                           "target_vertices must be 2000 to 65535 (65,535 is the game's absolute vertex limit)"))
    if not isinstance(out["provider"], str) or not out["provider"]:
        problems.append(tr("provider が空です", "provider is empty"))
    if out["multi_mode"] not in MULTI_MODES:
        problems.append(tr("multi_mode は stochastic / multidiffusion", "multi_mode must be stochastic or multidiffusion"))
    if out["sampler_preset"] not in SAMPLER_PRESETS:
        problems.append(tr("sampler_preset は standard / fast", "sampler_preset must be standard or fast"))
    if out["convert_texture_max"] not in CONVERT_TEXTURE_MAX:
        problems.append(tr("convert_texture_max は 512 / 1024 / 2048", "convert_texture_max must be 512, 1024 or 2048"))
    if problems:
        raise BackendError("INVALID_INPUT", tr("生成設定が不正です: ", "Invalid generation settings: ") + "; ".join(problems),
                           params={"problems": problems})
    return out


def validate(data: dict) -> dict:
    if data is not None and not isinstance(data, dict):
        # 型の誤りは BackendError にする。以前は TypeError などが抜け、PUT では INTERNAL、設定ファイルでは起動の失敗に
        # なっていた（点検 R09A-02）
        raise BackendError("INVALID_INPUT", tr("設定は JSON のオブジェクトです", "The settings must be a JSON object"))
    data = dict(data or {})
    try:
        old = int(data.get("version") or 1) < SETTINGS_VERSION
    except (TypeError, ValueError, OverflowError):
        old = True
    if old:
        # 版 1 の build_languages（既定 ["EN"] が保存されている）は「Text_JA.arc を作るか」の意味だった。版 2 では
        # 「Text_JA.arc に日本語名を入れるか」なので、既定（入れる）に戻す（点検 C124）
        data.pop("build_languages", None)
        data["version"] = SETTINGS_VERSION
    out = _merge(DEFAULTS, data)
    for k in _PATH_KEYS:
        v = out.get(k) or ""
        if not isinstance(v, str):
            raise BackendError("INVALID_INPUT", tr(f"{k} は文字列で指定してください", f"{k} must be a string"))
        # 空かは引用符・空白を外した後で決める（'""' が normpath で "." になり、自動検出も止まっていた）。相対パス・
        # ドライブ相対（"D:Games"）・ルート相対（"\GD"）はバックエンドの作業フォルダー基準になるので断る。os.path.isabs は
        # "\GD" を通すので、ドライブとルートの両方を要る PureWindowsPath で判定する（点検 R09A-03）
        s = v.strip().strip('"').strip()
        if s and not pathlib.PureWindowsPath(s).is_absolute():
            raise BackendError("INVALID_INPUT", tr(f"{k} は絶対パス（例 C:\\...）で指定してください",
                                                   f"{k} must be an absolute path (e.g. C:\\...)"),
                               params={"reason": "path_not_absolute", "key": k})
        out[k] = os.path.normpath(s) if s else ""
    for k in _BOOL_KEYS:
        out[k] = bool(out[k])
    if not isinstance(out.get("build_languages") or [], (list, tuple)):
        raise BackendError("INVALID_INPUT", tr("build_languages は EN（必須）と JA の組み合わせです",
                                               "build_languages must be a combination of EN (required) and JA"))
    langs = [str(x).upper() for x in (out.get("build_languages") or ["EN"])]
    if "EN" not in langs or any(x not in ("EN", "JA") for x in langs):
        raise BackendError("INVALID_INPUT", tr("build_languages は EN（必須）と JA の組み合わせです",
                                               "build_languages must be a combination of EN (required) and JA"))
    out["build_languages"] = list(dict.fromkeys(langs))
    out["generation"] = validate_generation(out.get("generation") or {})
    return {k: out[k] for k in DEFAULTS}


def _parse_int(s: str):
    """設定ファイルの整数。Python の上限（4300 桁）を超えるものは文字列のまま返し、その項目の検査で既定値に戻す
    （以前は ValueError でファイル全体が JSON として読めない扱いになり、全項目を既定値に戻した。点検 R09B-03）。"""
    try:
        return int(s)
    except ValueError:
        return s


def _salvage(data) -> tuple[dict, list[str]]:
    """検査に通らない設定から、検査に通る項目だけを残す → (検査済みの設定, 既定値に戻した項目の名前)。

    項目ごと（generation はその中の値ごと）に :func:`validate` / :func:`validate_generation` で試す（点検 R09B-04）。"""
    if not isinstance(data, dict):
        return validate({}), []
    keep: dict = {"version": data.get("version")}
    dropped: list[str] = []
    for k in DEFAULTS:
        if k in ("version", "generation") or k not in data:
            continue
        try:
            validate({**keep, k: data[k]})
        except Exception:   # noqa: BLE001
            dropped.append(k)
            continue
        keep[k] = data[k]
    g = data.get("generation")
    if isinstance(g, dict):
        keep["generation"] = {}
        for k, v in g.items():
            try:
                validate_generation({k: v})
            except Exception:   # noqa: BLE001
                dropped.append(f"generation.{k}")
                continue
            keep["generation"][k] = v
    elif g:
        dropped.append("generation")
    try:
        return validate(keep), dropped
    except Exception:   # noqa: BLE001  念のため（項目ごとに通ったものは全体でも通るはず）
        return validate({}), []


class Settings:
    """設定の読み書き（スレッド安全）。"""

    def __init__(self, path: str | None = None):
        self.path = path or settings_path()
        self._lock = threading.Lock()
        self._unread = False        # 起動時にファイルを読めず既定値で動いている（読めるようになったら読み直す。R09B-04）
        self._data = self._load()

    def _load(self, tries: int = 3) -> dict:
        if not os.path.isfile(self.path):
            return validate({})
        # 読めない・検査に通らない設定は既定値に戻す（次の保存で書き直す）。以前は 1 項目の誤り・一時的な読み取りの失敗でも
        # 黙って全項目を既定値にし、次の保存でゲームの場所・TRELLIS 実行環境の場所などが失われた（点検 R09B-04）:
        # 読み取りの失敗は少し待って読み直し、検査に通らない値はその項目（生成設定はその値）だけを既定値に戻す。
        # 元のファイルは .bad-<日時> に写し、理由をログに残す
        for attempt in range(tries):
            try:
                # utf-8-sig: メモ帳の「UTF-8 (BOM 付き)」などで手で直したファイルも読む（以前は JSON として読めない扱いで
                # 全項目を既定値に戻した。点検 R09A-04）。書き込みは今までどおり BOM なし
                with open(self.path, encoding="utf-8-sig") as f:
                    data = json.load(f, parse_int=_parse_int)
                self._unread = False
                break
            except OSError as e:
                if attempt < tries - 1:
                    time.sleep(0.2)
                    continue
                if not self._unread:
                    self._report(tr(f"設定ファイルを読めないため、既定値で動いています（{type(e).__name__}）",
                                    f"Cannot read the settings file, so the defaults are used ({type(e).__name__})"),
                                 backup=False)
                self._unread = True
                return validate({})
            except (ValueError, RecursionError) as e:
                # JSON として読めない（文字コードの誤りを含む）。RecursionError は深く入れ子にした配列・オブジェクトで、
                # 以前は起動の失敗になり、ログにも .bad の写しにも残らなかった（点検 R09B-03）
                self._unread = False
                self._report(tr(f"設定ファイルが JSON として読めないため、既定値に戻しました（{type(e).__name__}）",
                                f"The settings file cannot be read as JSON, so the defaults were restored "
                                f"({type(e).__name__})"))
                return validate({})
        try:
            return validate(data)
        except Exception as e:      # noqa: BLE001  検査に通らない（型の誤りを含む）→ 通る項目だけ残す
            reason = e.message if isinstance(e, BackendError) else f"{type(e).__name__}: {e}"
        out, dropped = _salvage(data)
        names = ", ".join(dropped) or tr("（全体）", "(all)")
        self._report(tr(f"設定ファイルの値が不正なため、次の項目を既定値に戻しました: {names}（{reason}）",
                        f"The settings file has invalid values, so these items were reset to the defaults: {names} "
                        f"({reason})"))
        return out

    def _report(self, message: str, backup: bool = True) -> None:
        """壊れた設定ファイルを .bad-<日時> に写し（同じ内容の写しがあれば写さない）、理由をログに残す。失敗は無視する。"""
        if backup:
            try:
                with open(self.path, "rb") as f:
                    raw = f.read()
                same = False
                for p in glob.glob(glob.escape(self.path) + ".bad-*"):
                    with open(p, "rb") as f:
                        if f.read() == raw:
                            same = True
                            break
                if not same:
                    shutil.copy2(self.path, f"{self.path}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
                    message += tr("。元のファイルは backend_settings.json.bad-<日時> に残しました",
                                  ". The original file was kept as backend_settings.json.bad-<date and time>")
            except OSError:
                pass
        try:
            logs.warning(message, code="INVALID_INPUT")
        except (OSError, ValueError):
            pass

    def _reload_if_unread(self) -> None:
        # 起動時に読めなかったファイルを、既定値で上書きする前に読み直す（ロックの中で呼ぶ。R09B-04）
        if self._unread and os.path.isfile(self.path):
            self._data = self._load(tries=1)     # 読めないままのときに毎回待たない

    def get(self) -> dict:
        with self._lock:
            self._reload_if_unread()
            return copy.deepcopy(self._data)

    def update(self, partial: dict) -> dict:
        if not isinstance(partial, dict):
            raise BackendError("INVALID_INPUT", tr("設定は JSON のオブジェクトで送ってください", "Send the settings as a JSON object"))
        unknown = sorted(set(partial) - set(DEFAULTS))
        if unknown:
            raise BackendError("INVALID_INPUT", tr(f"不明な設定項目: {unknown}", f"Unknown settings: {unknown}"))
        with self._lock:
            self._reload_if_unread()
            new = validate(_merge(self._data, partial))
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(new, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                # 書きかけの .tmp を残さない（UTF-8 にできない文字・ディスクの空きなど。元のファイルはそのまま。点検 R09B-02）
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            self._unread = False
            self._data = new
            return copy.deepcopy(new)


# ---------------------------------------------------------------------------
# 場所の解決
# ---------------------------------------------------------------------------
def detect_game_dirs() -> list[str]:
    from modbuilder import steam
    try:
        return [f["path"] for f in steam.find_grim_dawn()]
    except Exception:   # noqa: BLE001  Steam が無い・レジストリが読めない
        return []


def resolve_game_dir(s: dict) -> str | None:
    """Grim Dawn のインストール先（ゲームデータの読み取り用）。見つからなければ None。"""
    from modbuilder.gamedata import looks_like_gd
    if s.get("game_dir"):
        return s["game_dir"] if looks_like_gd(s["game_dir"]) else None
    for d in detect_game_dirs():
        if looks_like_gd(d):
            return d
    return None


def resolve_install_target(s: dict) -> tuple[str | None, bool]:
    """(MOD の導入先, テスト用の導入先か)。"""
    if s.get("install_target_dir"):
        return s["install_target_dir"], True
    return resolve_game_dir(s), False


def trellis_root_candidates(s: dict) -> list[str]:
    """TRELLIS 実行環境を探す場所（先頭から）。設定の trellis_root があればそれだけ。

    自動のとき: <データ>\\trellis-runtime、次にセットアップの既定の取得先（利用者のフォルダー名に全角文字があるなどで
    データフォルダーの中に置けない PC の %SystemDrive%\\GDSkinStudio\\trellis-runtime。setup.default_root と同じ規則。
    以前の版はそこへ取得した後に設定を空で保存していたため、配布物でも探す。点検 N01）。開発時はリポジトリの隣も探す
    （配布物は展開したフォルダーの隣は探さない）。"""
    if s.get("trellis_root"):
        return [s["trellis_root"]]
    from .setup import default_root
    out = [os.path.join(paths.home(), "trellis-runtime")]
    d = default_root()
    if os.path.normcase(os.path.abspath(d)) != os.path.normcase(os.path.abspath(out[0])):
        out.append(d)
    if not paths.IS_DIST:
        out.append(os.path.join(os.path.dirname(paths.REPO_ROOT), "trellis-runtime"))
    return out


def resolve_trellis_root(s: dict) -> str | None:
    for c in trellis_root_candidates(s):
        if os.path.isfile(os.path.join(c, "venv", "Scripts", "python.exe")):
            return c
    return None
