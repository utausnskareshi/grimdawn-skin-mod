"""生成結果の下書き（``<データ>\\work\\<下書きID>\\``。docs/app_architecture.md 5.2 節）。

下書きは装備として保存する前の生成・変換結果で、``draft.json`` に入力・設定・各工程の状態を記録する。
入力（プロンプト・画像のコピー）は利用者のデータとしてここに置き、ログには残さない。
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import secrets
import shutil
import stat
import sys
import threading
import time

from . import logs, paths
from .errors import BackendError
from .i18n import tr

DRAFT_FORMAT = "gdskin-draft"
# 処理中の状態（生成・変換・装備の変換し直しの途中）。起動時にこれらのまま残っていれば中断の扱いにする
ACTIVE_STATUSES = ("created", "generating", "converting", "reconverting")
_lock = threading.Lock()
# draft.json の置き換えの再試行（PermissionError のときだけ）。Windows では置き換え先を別のスレッド（一覧・データの整理の
# 読み込み）やほかのソフトが開いている間、os.replace が拒否される（点検 R10B-03）
REPLACE_TRIES, REPLACE_DELAY = 10, 0.05
# 下書きの削除の再試行。取消の直後などで使用中のファイル（候補の生成結果の .pt など）が閉じるのを少し待つ（点検 R10B-02）
DELETE_TRIES, DELETE_DELAY = 3, 0.2


def _replace(src: str, dst: str) -> None:
    """os.replace。置き換え先が一時的に開かれている（PermissionError）ときだけ短い間隔でやり直し、最後は例外を上げる。"""
    for i in range(REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_DELAY)


def _is_reparse(st) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def is_link(path: str) -> bool:
    """path 自体がリンク（シンボリックリンク・ジャンクション）か（無ければ False）。shutil の rmtree と同じ判定で、
    OneDrive などのほかの再解析ポイント（クラウドのフォルダー）はリンクとして扱わない（点検 R10A-01・R10B-01）。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or (
        _is_reparse(st) and getattr(st, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003))


def remove_tree(d: str) -> None:
    """下書きのフォルダーを消す。中身を先に消し、draft.json は最後に消す（途中で消せなくても draft.json が残り、一覧・
    データの整理に出るので、もう一度消せる。点検 UD1-R5・R10B-02）。リンク・ジャンクションはたどらない。失敗は OSError。
    d 自体がリンク・ジャンクションなら、リンクだけを外してリンク先の中身には触れない（点検 R10A-01・R10B-01）。

    消せないファイル（使用中など）があっても止めずに、ほかの項目は消せるだけ消す（以前の rmtree(ignore_errors=True) と
    同じく、残るのは消せなかったものだけ。点検 D2_backend-01）。失敗があれば draft.json とフォルダーを残し、最初の失敗を上げる。"""
    if is_link(d):
        # 利用者が下書きを別の場所へ移してつないだ、など。リンク先は作業フォルダーの外なので消さない
        # （setup の maintenance._remove_path と同じ扱い）
        try:
            os.unlink(d)
        except OSError:
            os.rmdir(d)
        return
    first: list[BaseException] = []

    def keep(_func, _path, exc):         # rmtree の中の失敗を覚えて続ける（3.12 以降は onexc、それより前は onerror）
        first.append(exc[1] if isinstance(exc, tuple) else exc)

    rm_kw = {"onexc": keep} if sys.version_info >= (3, 12) else {"onerror": keep}
    for e in list(os.scandir(d)):
        if e.name == "draft.json":
            continue
        try:
            if e.is_dir(follow_symlinks=False) and not e.is_symlink() and not _is_reparse(e.stat(follow_symlinks=False)):
                shutil.rmtree(e.path, **rm_kw)
            else:
                os.remove(e.path)        # ファイル・リンク（リンク先はたどらない）
        except OSError as x:
            first.append(x)
    if first:
        err = first[0]
        raise err if isinstance(err, OSError) else OSError(tr(f"下書きの中身を消せません: {err!r}",
                                                              f"Could not delete the contents of the draft: {err!r}"))
    p = os.path.join(d, "draft.json")
    if os.path.exists(p):
        os.remove(p)
    os.rmdir(d)


class Draft:
    def __init__(self, data: dict, directory: str):
        self.data = data
        self.dir = directory

    @property
    def id(self) -> str:
        return self.data["id"]

    def path(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)

    @property
    def input_dir(self) -> str:
        return self.path("input")

    @property
    def generate_dir(self) -> str:
        return self.path("generate")

    @property
    def convert_dir(self) -> str:
        return self.path("convert")

    def files(self) -> dict:
        """画面のプレビューに使うファイル（存在するものだけ。絶対パス）。"""
        out = {}
        gen = _obj(self.data.get("generation"))
        conv = _obj(self.data.get("conversion"))
        image = _obj(self.data.get("input")).get("image")
        cands = {
            "generated_glb": gen.get("glb"),
            "candidates_png": _obj(gen.get("files")).get("candidates_png"),
            "preprocessed_png": _obj(gen.get("files")).get("preprocessed_png"),
            "preview_m_glb": _obj(conv.get("files")).get("bind_m"),
            "preview_f_glb": _obj(conv.get("files")).get("bind_f"),
            "preview_png": _obj(conv.get("files")).get("preview"),
            "input_image": image.get("file") if isinstance(image, dict) else None,
        }
        for k, rel in cands.items():
            if rel and isinstance(rel, str):
                p = self.path(rel.replace("/", os.sep))
                if os.path.isfile(p):
                    out[k] = p
        return out

    def summary(self, colors: bool = True) -> dict:
        """画面の下書きの要約。外部の編集・古い版などで形の崩れた draft.json（errors が null、id が無い、input が配列など）
        でも例外にしない（1 件で GET /drafts の全体が失敗し、ほかの下書きも復元できなくなっていた。点検 R10B-05）。
        id が文字列でなければフォルダー名を使う（recover_interrupted と同じ）。colors が False なら候補の色を測らない
        （candidates.view。候補の色の印と注意 colorless_* は出ない。2026-11-18 点検 BC-02）。"""
        from . import candidates          # 候補の一覧と保存した生成結果（SF06・SF14。U-A2）
        d = self.data
        inp, gen, conv = _obj(d.get("input")), _obj(d.get("generation")), _obj(d.get("conversion"))
        recs = _seq(inp.get("images")) or ([inp["image"]] if isinstance(inp.get("image"), dict) else [])
        cands = candidates.view(self, colors=colors)
        warnings = list(_seq(gen.get("warnings"))) + list(_seq(conv.get("warnings")))
        # 今の体がほぼ無彩色のときの注意（B-040。記録せず、要約のたびに今の体で決める。候補を選び直すと変わる）
        if cands and cands.get("color_notice") and cands["color_notice"] not in warnings:
            warnings.append(cands["color_notice"])
        return {"id": d["id"] if isinstance(d.get("id"), str) else os.path.basename(self.dir),
                "status": d.get("status"), "created_at": d.get("created_at"),
                "updated_at": d.get("updated_at"), "kind": inp.get("kind"), "text": inp.get("text"),
                "provider": d.get("provider"), "model_id": d.get("model_id"), "seed": d.get("seed"),
                "settings": d.get("settings"), "replace_equipment_id": d.get("replace_equipment_id"),
                "job_id": d.get("job_id"),
                # 装備として保存済み（keep_drafts で残した下書き。library_ops.save_draft。点検 R18A-02）。オブジェクトか null
                "saved": d.get("saved") if isinstance(d.get("saved"), dict) else None,
                "quality_flags": conv.get("quality_flags"),
                "warnings": warnings,
                "conversion": {k: conv.get(k) for k in ("status", "vertices", "triangles")},
                "yaw_deg": conv.get("yaw_deg") or 0,      # 変換の前に回した角度（12 章 #16）
                "pose": _obj(gen.get("info")).get("pose"),
                "images": [{"label": r.get("label"), "path": self.path(*r["file"].split("/")), "has_alpha": r.get("has_alpha")}
                           for r in recs if isinstance(r, dict) and isinstance(r.get("file"), str)
                           and os.path.isfile(self.path(*r["file"].split("/")))],
                "preprocessed_images": [p for p in (self.path(*x.split("/")) for x in _seq(gen.get("preprocessed"))
                                                    if isinstance(x, str))
                                        if os.path.isfile(p)],
                "multi_mode": _obj(d.get("settings")).get("multi_mode") if len(_seq(inp.get("images"))) > 1 else None,
                "candidates": cands,
                # 見た目の作り直し（SF10）の元の装備（入力の種類 variant の下書きだけ。写した GLB の場所は出さない）
                **({"variant_of": {k: v for k, v in inp["variant"].items() if k != "file"}}
                   if inp.get("kind") == "variant" and isinstance(inp.get("variant"), dict) else {}),
                "errors": _seq(d.get("errors"))[:5], "files": self.files(), "dir": self.dir}


def _obj(v) -> dict:
    """draft.json の中の辞書のはずの値（形が崩れていれば空の辞書。点検 R10B-05）。"""
    return v if isinstance(v, dict) else {}


def _seq(v) -> list:
    """draft.json の中の配列のはずの値（形が崩れていれば空の配列。点検 R10B-05）。"""
    return v if isinstance(v, list) else []


class DraftStore:
    def __init__(self, root: str | None = None):
        self.root = root or paths.work_dir()

    def _dir(self, draft_id: str) -> str:
        # 文字列でない ID（JSON の数値など）も不正な ID として扱う（以前は TypeError でジョブが INTERNAL になった。点検 X1X-01）
        if (not isinstance(draft_id, str) or not draft_id or any(c in draft_id for c in "\\/:.")
                or not draft_id.startswith("d")):
            raise BackendError("NOT_FOUND", tr(f"下書きID が不正です: {draft_id}", f"Invalid draft ID: {draft_id}"))
        return os.path.join(self.root, draft_id)

    def create(self, kind: str, provider: str | None, settings: dict, replace_equipment_id: str | None = None,
               job_id: str | None = None) -> Draft:
        with _lock:
            stamp = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
            while True:
                did = f"d{stamp}{secrets.token_hex(3)}"
                d = os.path.join(self.root, did)
                if not os.path.exists(d):
                    break
            for sub in ("input", "generate", "convert"):
                os.makedirs(os.path.join(d, sub), exist_ok=True)
        data = {"format": DRAFT_FORMAT, "version": 1, "id": did, "created_at": logs.now_iso(), "updated_at": None,
                "status": "created", "input": {"kind": kind}, "provider": provider, "model_id": None, "seed": None,
                "settings": settings, "generation": {}, "conversion": {}, "replace_equipment_id": replace_equipment_id,
                "job_id": job_id, "errors": []}
        dr = Draft(data, d)
        self.save(dr)
        return dr

    def save(self, dr: Draft) -> None:
        dr.data["updated_at"] = logs.now_iso()
        p = dr.path("draft.json")
        tmp = p + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(dr.data, f, ensure_ascii=False, indent=1)
            _replace(tmp, p)
        except BaseException:
            # 書きかけの .tmp を残さない（UTF-8 にできない文字の UnicodeEncodeError・ディスクの空きなど。元の draft.json は
            # そのまま。点検 R10A-04）
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise

    def get(self, draft_id: str) -> Draft:
        d = self._dir(draft_id)
        p = os.path.join(d, "draft.json")
        if not os.path.isfile(p):
            raise BackendError("NOT_FOUND", tr(f"下書きがありません: {draft_id}", f"Draft not found: {draft_id}"))
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("format") != DRAFT_FORMAT:
            raise BackendError("NOT_FOUND", tr(f"下書きの形式が違います: {draft_id}", f"Unexpected draft format: {draft_id}"))
        return Draft(data, d)

    def list(self) -> list[Draft]:
        out = []
        if os.path.isdir(self.root):
            for name in sorted(os.listdir(self.root), reverse=True):
                if name.startswith("d") and os.path.isfile(os.path.join(self.root, name, "draft.json")):
                    try:
                        out.append(self.get(name))
                    except (BackendError, OSError, ValueError):
                        continue
        return out

    def delete(self, draft_id: str) -> None:
        """下書きを消す（例外は出さない）。使用中のファイルで消し切れなければ少し待ってやり直し、それでも残ったら
        draft.json を残して status を failed にする（データの整理から消せる。ready のまま残すと、中身の欠けた下書きを
        画面が前回の下書きとして復元しうる。点検 R10B-02）。"""
        d = self._dir(draft_id)
        err = None
        for i in range(DELETE_TRIES):
            if not os.path.isdir(d) and not is_link(d):       # リンク先の無いジャンクションも外す
                return
            try:
                remove_tree(d)
                return
            except OSError as e:
                err = e
                if i < DELETE_TRIES - 1:
                    time.sleep(DELETE_DELAY)
        logs.warning(tr(f"下書きを消し切れませんでした: {draft_id}（{type(err).__name__}）。データの整理から消せます",
                        f"Could not delete the draft completely: {draft_id} ({type(err).__name__}). "
                        f"It can be deleted in data cleanup"))
        try:
            dr = self.get(draft_id)
            if dr.data.get("status") != "failed":
                dr.data["status"] = "failed"
                if not isinstance(dr.data.get("errors"), list):
                    dr.data["errors"] = []
                self.add_error(dr, "delete", "DELETE_INCOMPLETE",
                               tr(f"下書きを消し切れませんでした（{type(err).__name__}。使用中のファイル）",
                                  f"Could not delete the draft completely ({type(err).__name__}; a file is in use)"))
                self.save(dr)
        except (BackendError, OSError, ValueError):
            pass                         # draft.json も消えた（空のフォルダーだけが残る）・読めない

    def add_error(self, dr: Draft, stage: str, code: str, message: str) -> None:
        dr.data.setdefault("errors", []).insert(0, {"at": logs.now_iso(), "stage": stage, "code": code, "message": message})
        del dr.data["errors"][20:]

    def recover_interrupted(self) -> list[str]:
        """起動時の後始末: 処理中の状態のまま残った下書き（前のバックエンドの強制終了・異常終了の後）を「中断」にする
        → 直した下書きの ID（2026-10-17 監査 C104）。

        status を ``failed`` にし、errors の先頭に ``INTERRUPTED``（中断。stage は元の状態）を記録する。消さない
        （利用者の判断 2026-10-17: 自動では消さず、データの整理から消せるようにする）。装備の変換し直しの作業用の下書き
        （kind ``reconvert``）も同じ扱い（装備は変わっていない）。このバックエンドの処理がまだ無い起動時に呼ぶ前提
        （同じデータフォルダーのバックエンドは 1 つだけ。run_backend のロックと WPF の Mutex）。

        JSON としては読めても形の崩れた draft.json（``errors`` が null、``id`` が無いなど）で起動ごと止めないよう、
        下書きごとに例外を捕まえて飛ばす（フォルダー名と例外の型だけをログに残す）。ID はフォルダー名を使う。"""
        fixed = []
        for dr in self.list():
            name = os.path.basename(dr.dir)
            try:
                st = dr.data.get("status")
                if st not in ACTIVE_STATUSES:
                    continue
                dr.data["status"] = "failed"
                conv = dr.data.get("conversion")
                if isinstance(conv, dict) and conv:      # 変換の途中なら出力は途中の状態（run_reconvert の失敗と同じ扱い）
                    conv["status"] = "failed"
                if not isinstance(dr.data.get("errors"), list):
                    dr.data["errors"] = []
                self.add_error(dr, str(st), "INTERRUPTED",
                               tr(f"バックエンドが処理の途中で終了したため、中断しました（前の状態 {st}）",
                                  f"Interrupted because the backend exited during processing (previous status {st})"))
                self.save(dr)
            except OSError:
                continue
            except Exception as e:  # noqa: BLE001 — 1 件の崩れた下書きで起動を止めない
                logs.warning(tr(f"処理中の下書きを確かめられないため飛ばしました: {name}（{type(e).__name__}）",
                                f"Skipped a draft left in progress that could not be checked: {name} ({type(e).__name__})"))
                continue
            fixed.append(name)
        return fixed
