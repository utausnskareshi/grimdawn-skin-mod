"""作業プロセスの入口: ``python -I -X utf8 worker.py <作業> <引数の JSON ファイル>``。

標準出力に ``@@GDSKIN {...}`` の行（progress / log / result / error）を書く（docs/app_architecture.md 3.3 節）。
終了コード: 0 成功、1 分類済みのエラー、2 想定外のエラー。
"""
from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gdskin_backend import paths  # noqa: E402  sys.path に src を入れる

paths.ensure_sys_path()

from gdskin_backend.errors import BackendError, classify_output  # noqa: E402
from gdskin_backend.i18n import tr  # noqa: E402
from gdskin_backend import worker_tasks  # noqa: E402


def emit(type_: str, **kw) -> None:
    sys.stdout.write("@@GDSKIN " + json.dumps({"type": type_, **kw}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(argv) != 2:
        emit("error", code="INTERNAL", message=tr("使い方: worker.py <作業> <引数の JSON>",
                                                  "Usage: worker.py <task> <parameter JSON file>"))
        return 2
    task_name, ppath = argv
    tasks = dict(worker_tasks.TASKS)
    if os.environ.get("GDSKIN_WORKER_SELFTEST") == "1":
        tasks["selftest"] = worker_tasks.task_selftest
    fn = tasks.get(task_name)
    if fn is None:
        emit("error", code="INTERNAL", message=tr(f"作業が不明です: {task_name}", f"Unknown task: {task_name}"))
        return 2
    with open(ppath, encoding="utf-8") as f:
        params = json.load(f)
    try:
        result = fn(params, emit)
    except BackendError as e:
        emit("error", **e.to_dict())
        return 1
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        emit("error", code=classify_output(tb, "INTERNAL"), message=f"{type(e).__name__}: {e}",
             detail="\n".join(tb.splitlines()[-25:]))
        return 2
    emit("result", data=result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
