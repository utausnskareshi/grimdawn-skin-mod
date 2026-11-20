"""作業プロセス（``worker.py``）で動く処理: 変換（gdconvert）、MOD のビルド（modbuilder）、基準メッシュの取り出し。

各処理は ``task(params, emit)``。``emit(type, **kw)`` で進捗・ログを送り、結果の dict を返す。
分類できる失敗は :class:`~gdskin_backend.errors.BackendError` にする（worker.py が ``error`` 行にする）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import traceback

from .errors import BackendError
from .i18n import tr


def convert_steps() -> list[tuple[str, float, str]]:
    """gdconvert の工程の表示（pipeline.convert の log の先頭）→ (進捗, 段階)。pipeline は各工程の始めに「[n] …を開始」を
    出すので、進捗はその工程の始めの値（工程の終わりの行も同じ段階・同じ値になる）。

    gdconvert・modbuilder はこのプロセスの表示言語（GDSKIN_LANG）で書くので、文の目印（終わりの行の先頭、注意・警告の語
    など）は使うときに表示言語のものを選ぶ（英語は gdconvert の pipeline.py・refs.py、modbuilder の builder.py の tr の
    英語の文と同じ）。"""
    return [
        ("[1]", 0.02, "convert_load"),
        ("[2]", 0.08, "convert_orient"),
        ("[3]", 0.12, "convert_reduce"),
        ("[4]", 0.30, "convert_texture"),
        ("[5m]", 0.40, "convert_fit_m"),
        ("[5f]", 0.66, "convert_fit_f"),
        ("[6]", 0.92, "convert_preview"),
        (tr("完了", "Done"), 1.0, "convert_done"),
    ]


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def refs_unreadable_message() -> str:
    """基準ファイルを開けない（REFS_MISSING の reason refs_unreadable）ときの文。生成の事前確認（pipeline.preflight）と
    変換（task_convert）で同じ文にする（2026-11-04 退行の確認 2 回目 B1）。"""
    return tr("基準メッシュを開けません（ほかのアプリが開いていないか、読み取りの権限を確かめてから、やり直してください）",
              "The reference meshes cannot be opened (check that no other app has them open and that you have permission "
              "to read them, then try again)")


def _classify_convert(tb: str, stage: str | None, message: str = "") -> str:
    """変換の例外 → エラーコード（トレースバックの中のモジュール、例外の文、失敗した工程で判断）。"""
    if "gdconvert\\rigging.py" in tb or "gdconvert/rigging.py" in tb or "gdconvert\\skeleton.py" in tb \
            or "gdconvert/skeleton.py" in tb:
        return "RIGGING_FAILED"
    if any(k in message for k in (".tex", "TextureCompiler", ".msh", tr("頂点数", "vertex count"))):
        return "EXPORT_FAILED"           # pipeline.convert が出力の検証で止めた（.tex の検証、頂点数の上限）
    if any(m in tb for m in ("gdconvert\\exporter.py", "gdconvert/exporter.py", "gdmesh\\", "gdmesh/",
                             "gdconvert\\texture.py", "gdconvert/texture.py")):
        return "EXPORT_FAILED"
    if "gdconvert\\gltf.py" in tb or "gdconvert/gltf.py" in tb:
        return "INVALID_MODEL"
    return {"convert_load": "INVALID_MODEL", "convert_texture": "EXPORT_FAILED", "convert_fit_m": "RIGGING_FAILED",
            "convert_fit_f": "RIGGING_FAILED"}.get(stage or "", "CONVERSION_FAILED")


def task_convert(params: dict, emit) -> dict:
    """GLB → 男女の .msh・.tex・確認画像・バインド姿勢の GLB。``convert_samples.py`` と同じ形式の manifest.json を書く。

    読めない GLB（gdconvert.gltf.GltfError）は INVALID_MODEL にし、理由のコード（未対応の拡張・圧縮など）を
    ``params.reason`` に入れる（画面の文は Error_INVALID_MODEL_<reason>_*）。読み込みの注意のキー（``load_*``）は
    結果と manifest.json の ``load_flags`` に入れる（下書き・装備一覧の注意になる）。"""
    from gdconvert.config import AxisConfig, ConvertConfig
    from gdconvert.gltf import GltfError
    from gdconvert.pipeline import TexVerifyError, convert
    from gdconvert.refs import RefsCorruptError, missing_refs, ref_problems

    glb, out_dir, name = params["glb"], params["out_dir"], params.get("name", "model")
    refs_dir, gd = params["refs_dir"], params.get("gd")
    missing = missing_refs(refs_dir)
    if missing:
        raise BackendError("REFS_MISSING", tr(f"基準メッシュがありません: {missing}", f"The reference meshes are missing: {missing}"),
                           params={"missing": missing})
    # あるが読めない（切れた・0 で埋まった）基準ファイルも、TextureCompiler と変換の前に止める。画面の REFS_MISSING の
    # 案内（ゲームから準備する）で取り出し直せば直る（点検 R03A-01）。開けない（ほかのソフトのロック・アクセス権）ファイルは
    # 壊れているとは言わず、pipeline.preflight と同じ形（reason refs_unreadable）にする（2026-11-04 退行の確認 2 回目 B1）
    problems = ref_problems(refs_dir)
    corrupt = [k for k, v in problems.items() if v != "unreadable"]
    if corrupt:
        raise BackendError("REFS_MISSING", tr(f"基準メッシュが壊れています: {corrupt}", f"The reference meshes are corrupted: {corrupt}"),
                           params={"reason": "refs_corrupt", "corrupt": corrupt})
    locked = [k for k, v in problems.items() if v == "unreadable"]
    if locked:
        raise BackendError("REFS_MISSING", refs_unreadable_message(), params={"reason": "refs_unreadable", "unreadable": locked})
    # .tex を作るゲーム付属のツール。無いと男女の当てはめと確認画像の後で EXPORT_FAILED になるので、変換の前に止める
    # （バックエンドの pipeline.preflight・require_texture_compiler と同じ確認。この処理を直接呼ぶ場合のため。点検 C55）
    tool = os.path.join(gd or "", "TextureCompiler.exe")
    if params.get("make_tex", True) and not (gd and os.path.isfile(tool)):
        raise BackendError("GAME_NOT_FOUND", tr("Grim Dawn のフォルダーに TextureCompiler.exe がありません（変換で .tex を作るのに必要）",
                                                "TextureCompiler.exe is not in the Grim Dawn folder (needed to make .tex "
                                                "files during conversion)"),
                           params={"reason": "texture_compiler_missing", "missing": "TextureCompiler.exe", "path": tool})
    # テクスチャの最大辺（SF24。設定 convert_texture_max。指定なし = ConvertConfig の既定 2048 で今までと同じ）
    cfg = ConvertConfig(target_vertices=int(params.get("target_vertices", 25000)),
                        texture_max_size=int(params.get("texture_max_size") or ConvertConfig.texture_max_size))
    if params.get("axes"):
        cfg.axes = AxisConfig(**params["axes"])
    state = {"stage": None}

    def log(msg: str) -> None:
        s = msg.strip()
        for prefix, p, stage in convert_steps():
            if s.startswith(prefix):
                state["stage"] = stage
                emit("progress", progress=p, stage=stage)
                break
        emit("log", level="warning" if tr("注意", "Warning") in s else "info", message=s)

    if os.path.isdir(out_dir):
        for f in os.listdir(out_dir):          # 前回の変換の残り（設定を変えての再変換）
            fp = os.path.join(out_dir, f)
            if os.path.isfile(fp):
                os.remove(fp)
    t0 = time.time()
    try:
        rep = convert(glb, out_dir, name, refs_dir, gd, cfg, preview=True, make_tex=bool(params.get("make_tex", True)),
                      export_glb=True, log=log)
    except BackendError:
        raise
    except RefsCorruptError as e:       # 文に .msh が入るので、汎用の分類では EXPORT_FAILED になってしまう
        tb = traceback.format_exc()
        if getattr(e, "reason", "corrupt") == "unreadable":     # RefsUnreadableError（開けない・読めない。B1）
            raise BackendError("REFS_MISSING", refs_unreadable_message(), "\n".join(tb.splitlines()[-25:]),
                               {"stage": state["stage"], "reason": "refs_unreadable", "unreadable": [e.rel], "path": e.path})
        raise BackendError("REFS_MISSING", str(e), "\n".join(tb.splitlines()[-25:]),
                           {"stage": state["stage"], "reason": "refs_corrupt", "corrupt": [e.rel], "path": e.path})
    except GltfError as e:
        tb = traceback.format_exc()
        p = {"stage": state["stage"]}
        if e.reason:
            p.update(e.params, reason=e.reason)
        raise BackendError("INVALID_MODEL", f"{type(e).__name__}: {e}", "\n".join(tb.splitlines()[-25:]), p)
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        p = {"stage": state["stage"]}
        if isinstance(e, TexVerifyError):       # 画面の文を .tex の圧縮の確認の案内にする（点検 R04B-03）
            p["reason"] = e.reason
        raise BackendError(_classify_convert(tb, state["stage"], str(e)), f"{type(e).__name__}: {e}",
                           "\n".join(tb.splitlines()[-25:]), p)
    tex = rep["steps"]["texture"].get("tex")
    if params.get("make_tex", True) and not tex:
        raise BackendError("EXPORT_FAILED", tr(".tex を作れませんでした（TextureCompiler.exe が見つからない）",
                                               "Could not make the .tex file (TextureCompiler.exe was not found)"),
                           params={"gd": gd})
    files = {}
    for key, fname in (("msh_m", f"{name}_m.msh"), ("msh_f", f"{name}_f.msh"), ("tex", f"{name}_dif.tex"),
                       ("tex_source_png", f"{name}_dif.png"), ("report", f"{name}_report.json"),
                       ("preview", f"{name}_preview.png"), ("bind_m", f"{name}_m_bind.glb"), ("bind_f", f"{name}_f_bind.glb")):
        p = os.path.join(out_dir, fname)
        if os.path.isfile(p):
            files[key] = {"file": fname, "md5": _md5(p), "bytes": os.path.getsize(p)}
    flags = {g: rep["steps"][f"gender_{g}"]["quality"]["flags"] for g in ("m", "f") if f"gender_{g}" in rep["steps"]}
    load_flags = list(rep["steps"].get("load", {}).get("load_flags") or [])
    msh_m = rep["steps"].get("gender_m", {}).get("msh", {})
    manifest = {
        "name": name,
        "input_glb": os.path.abspath(glb),
        "converted": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "gdconvert_config": cfg.to_dict(),
        "files": files,
        "texture_path_in_msh": rep["steps"]["texture"].get("game_path"),
        "vertices": msh_m.get("vertices"),
        "triangles": msh_m.get("triangles"),
        "quality_flags": flags,
        "load_flags": load_flags,
    }
    for k in ("prompt", "seed", "model_id"):      # modbuilder の find_converted が生成情報として読む
        if params.get(k) is not None:
            manifest[k] = params[k]
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return {"out_dir": out_dir, "name": name, "files": {k: v["file"] for k, v in files.items()},
            "quality_flags": flags, "load_flags": load_flags, "vertices": msh_m.get("vertices"),
            "triangles": msh_m.get("triangles"), "seconds": round(time.time() - t0, 1),
            "timings": rep.get("timings_seconds")}


def build_steps() -> list[tuple[str, float, str]]:
    """modbuilder の手順の表示（builder.build の log の先頭）→ (進捗, 段階)。先頭の文は表示言語のもの（convert_steps と同じ）。"""
    return [(tr("[ビルド", "[Build"), 0.02, "build_start"), ("[1]", 0.1, "build_records"), ("[2]", 0.3, "build_validate"),
            ("[3]", 0.55, "build_archive"), ("[4]", 0.8, "build_verify"), (tr("[完了]", "[Done]"), 1.0, "build_done")]


def task_build(params: dict, emit) -> dict:
    """MOD を作る。失敗は params.reason で画面の文を分ける（2026-10-17 FG05-R1・R2）: MOD_BUILD_FAILED の
    validation（検証で問題）・stopped（ビルドの途中で止まった。ArchiveTool の失敗など）・empty（装備が無い）・無し
    （ビルドを始める前の指定の誤りなど）、GAME_NOT_FOUND の game_tool_missing（ArchiveTool.exe が無い。path）。"""
    from modbuilder import builder
    from modbuilder.errors import BuildStopped, GameDataError, GameToolMissing, ModBuilderError, ValidationFailed
    from modbuilder.gamedata import GameData
    from modbuilder.library import Library

    def log(msg: str) -> None:
        s = msg.strip()
        for prefix, p, stage in build_steps():
            if s.startswith(prefix):
                emit("progress", progress=p, stage=stage)
                break
        # 検証の結果の行（「エラー 0、警告 n」。英語は「Errors: 0, Warnings: n」）は警告が 0 件なら info
        warn = tr("警告", "Warning") in s and tr("警告 0", "Warnings: 0") not in s
        emit("log", level="warning" if warn else "info", message=s)

    try:
        lib = Library.open(params["library_dir"], create=True)
        if not [e for e in lib.list() if params.get("include_dev") or not e.dev_only]:
            raise BackendError("MOD_BUILD_FAILED", tr("MOD に入れる装備がありません（装備一覧が空です）",
                                                      "There is no equipment to put in the MOD (the Library is empty)"),
                               params={"empty": True, "reason": "empty"})
        gd = GameData(params["gd"])
        m = builder.build(lib, gd, out_dir=params.get("out_dir"), languages=tuple(params.get("languages") or ("EN",)),
                          include_dev=bool(params.get("include_dev")), log=log)
    except ValidationFailed as e:
        raise BackendError("MOD_BUILD_FAILED", str(e).splitlines()[0], "\n".join(e.problems[:40]),
                           {"problems": e.problems[:100], "reason": "validation"})
    except GameToolMissing as e:
        raise BackendError("GAME_NOT_FOUND", str(e), params={"reason": "game_tool_missing", "path": e.path or ""})
    except GameDataError as e:
        raise BackendError("GAME_NOT_FOUND", str(e))
    except BuildStopped as e:
        raise BackendError("MOD_BUILD_FAILED", str(e), params={"reason": "stopped"})
    except ModBuilderError as e:
        raise BackendError("MOD_BUILD_FAILED", str(e))
    v = m.get("validation") or {}
    return {"build_id": m["build_id"], "build_dir": m["build_dir"], "files": m["files"].get("basemods"),
            "equipment": [e["id"] for e in m["library"]["equipment"]], "excluded": m.get("excluded", []),
            "warnings": v.get("warnings", []), "checks": v.get("checks", {}), "languages": m.get("languages")}


def task_refs(params: dict, emit) -> dict:
    """基準メッシュ（公式の素体など）をゲームから取り出す。ゲームの .arc は読み取り専用で直接読む（gdconvert.refs。
    コピーも ArchiveTool も使わない）。旧版が残した .arc のコピー（``refs_dir/_arc_copy``、約 950 MB）は最初に消す。"""
    from gdconvert.refs import REF_FILES, missing_refs, prepare_refs, ref_problems

    refs_dir, gd = params["refs_dir"], params["gd"]
    copy_dir = os.path.join(refs_dir, "_arc_copy")
    freed = 0
    if os.path.isdir(copy_dir):                   # 旧版のコピー（取り消し・失敗で残ったものも）。基準の有無に関係なく消す
        for dirpath, _d, files in os.walk(copy_dir):
            for f in files:
                try:
                    freed += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        shutil.rmtree(copy_dir, ignore_errors=True)
    # 分母は prepare_refs が取り出す（取り出し直す）ファイルの数: 足りないものと壊れているもの（開けないものは飛ばすので
    # 入れない）。壊れたファイルだけを取り出し直すときも進捗が進む（2026-11-04 退行の確認 2 回目 B4・点検 D1_lib-02）
    need = missing_refs(refs_dir, tuple(REF_FILES))
    broken = [k for k, v in ref_problems(refs_dir, tuple(REF_FILES)).items() if v == "corrupt"]
    total = max(1, len(need) + len(broken))
    count = {"n": 0}
    # gdconvert.refs.prepare_refs の 1 件ごとの行（表示言語の文）: 足りないファイルは「取り出し: 」、壊れたファイルは
    # 「壊れている基準ファイルを置き換えた: 」（その前の「…を取り出し直す: 」の行は数えない）
    done_marks = (tr("取り出し: ", "Extracted: "), tr("壊れている基準ファイルを置き換えた: ", "Replaced the corrupted reference file: "))

    def log(msg: str) -> None:
        if msg.startswith(done_marks):
            count["n"] += 1
        emit("progress", progress=0.05 + 0.9 * min(count["n"], total) / total, stage="refs_extract")
        emit("log", level="info", message=msg)

    emit("progress", progress=0.02, stage="refs_extract")
    try:
        done = prepare_refs(refs_dir, gd, tuple(REF_FILES), log=log)
    except FileNotFoundError as e:
        raise BackendError("GAME_NOT_FOUND", str(e))
    except Exception as e:  # noqa: BLE001  項目が無い（KeyError）・壊れている（ValueError）など
        raise BackendError("EXPORT_FAILED", f"{type(e).__name__}: {e}", traceback.format_exc()[-2000:])
    return {"extracted": done, "missing": missing_refs(refs_dir), "refs_dir": refs_dir, "freed_bytes": freed}


def task_selftest(params: dict, emit) -> dict:
    """テスト用（環境変数 GDSKIN_WORKER_SELFTEST=1 のときだけ使える）。"""
    mode = params.get("mode", "ok")
    steps = int(params.get("steps", 3))
    for i in range(steps):
        emit("progress", progress=(i + 1) / steps, stage="selftest")
        emit("log", level="info", message=f"step {i + 1}/{steps}")
        time.sleep(float(params.get("delay", 0.0)))
    if mode == "crash":
        os._exit(3)
    if mode == "fail":
        raise RuntimeError("selftest failure")
    if mode == "error":
        raise BackendError(params.get("code", "CONVERSION_FAILED"), "selftest classified error")
    if mode == "print_oom":
        print("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", flush=True)
        os._exit(1)
    return {"mode": mode, "steps": steps}


TASKS = {"convert": task_convert, "build": task_build, "refs": task_refs}
