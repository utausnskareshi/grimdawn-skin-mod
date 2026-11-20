"""環境チェック（計画書 6.10 EnvironmentChecker、docs/app_architecture.md 9 章）。

項目ごとに ``{"id", "status": ok|warning|error|missing, "code", "message", "params"}`` を返す。``summary`` は
画面のボタンを使えるかの判定（生成・変換・ビルド・導入）。
"""
from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import providers
from .i18n import tr
from .procutil import CREATE_NO_WINDOW, process_running
from .providers.base import CheckItem
from .settings import resolve_game_dir, resolve_install_target, resolve_trellis_root

BACKEND_MODULES = {"numpy": "numpy", "scipy": "scipy", "PIL": "Pillow", "lz4": "lz4",
                   "fast_simplification": "fast-simplification", "xatlas": "xatlas"}
GAME_PROCESSES = ("Grim Dawn.exe", "Grim Dawn x64.exe", "GrimDawn.exe")
FREE_SPACE_WARN = 5 * 1024 ** 3
MIN_DRIVER_MAJOR = 570
# GPU メモリの判定（2026-10-17 点検 C03。定義はここだけで、生成の事前確認 pipeline.preflight も同じものを使う）。
# nvidia-smi の memory.total は名目より少なく出る（12 GB の RTX 4070 は 12,282 MiB、RTX 5070 は 12,227 MiB）ので、
# 名目の 12 GB・16 GB より 512 MiB 下で比べる。11 GiB（11,264 MiB）は 11 GB の RTX 2080 Ti の報告値と同じなので使わない
VRAM_MIN_MIB = 12 * 1024 - 512       # 11,776 MiB: これ未満は 3D 生成を使えない（error。最低条件 12 GB）
VRAM_REC_MIB = 16 * 1024 - 512       # 15,872 MiB: これ未満は注意（warning。TRELLIS の README は 16 GB 以上を必須としている。C127）
# compute capability（点検 C32）: xformers 0.0.30 の最低の PTX が compute_60 なので 6.0 未満は使えない（error）。
# 7.0 未満は spconv が NVRTC に切り替わり、確かめていない（warning）
MIN_COMPUTE_CAP = (6, 0)
WARN_COMPUTE_CAP = (7, 0)
# Microsoft Visual C++ 再頒布可能パッケージ（x64、v14）。TRELLIS 実行環境の部品（onnxruntime・xatlas は 14.44 でリンク）が
# System32 の msvcp140.dll などを読む。Grim Dawn と一緒に Steam が入れる 2015 Update 3（14.0.24215）だけの PC では足りない（点検 C31）
VC_REDIST_URL = "https://aka.ms/vc14/vc_redist.x64.exe"
VC_RUNTIME_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "vcomp140.dll")
VC_RUNTIME_MIN_DEFAULT = "14.44"      # setup_manifest.json の min_vc_runtime が読めないとき

_GPU_QUERY = "index,uuid,pci.bus_id,name,memory.total,memory.used,driver_version,compute_cap"
_GPU_QUERY_BASIC = "name,memory.total,memory.used,driver_version"


def _nvidia_smi(exe: str, query: str):
    return subprocess.run([exe, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                          capture_output=True, text=True, errors="replace", timeout=20, stdin=subprocess.DEVNULL,
                          creationflags=CREATE_NO_WINDOW)


def _cap(text: str) -> str | None:
    t = (text or "").strip()
    try:
        major, minor = t.split(".")
        return f"{int(major)}.{int(minor)}"
    except ValueError:
        return None          # [N/A] など


def gpu_info() -> dict | None:
    """nvidia-smi の結果（無ければ None）→ {"gpus": [{index, uuid, pci_bus_id, name, memory_total_mib, memory_used_mib,
    driver, compute_cap}]}。compute_cap などを返さない古い nvidia-smi では、名前・メモリ・ドライバーだけを読む。"""
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    if not os.path.isfile(exe):
        return None
    try:
        r = _nvidia_smi(exe, _GPU_QUERY)
        full = r.returncode == 0
        if not full:
            r = _nvidia_smi(exe, _GPU_QUERY_BASIC)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return {"gpus": [], "error": (r.stdout + r.stderr).strip()[:300]}
    return {"gpus": parse_gpu_lines(r.stdout, full)}


def parse_gpu_lines(text: str, full: bool = True) -> list[dict]:
    """nvidia-smi の CSV（--format=csv,noheader,nounits）→ GPU の一覧。名前に「,」があっても前後の列の数で読む。"""
    gpus = []
    for n, ln in enumerate(text.splitlines()):
        parts = [p.strip() for p in ln.split(",")]
        try:
            if full and len(parts) >= 8:
                gpus.append({"index": int(parts[0]), "uuid": parts[1], "pci_bus_id": parts[2],
                             "name": ", ".join(parts[3:-4]), "memory_total_mib": int(float(parts[-4])),
                             "memory_used_mib": int(float(parts[-3])), "driver": parts[-2], "compute_cap": _cap(parts[-1])})
            elif not full and len(parts) >= 4:
                gpus.append({"index": n, "uuid": None, "pci_bus_id": None, "name": ", ".join(parts[:-3]),
                             "memory_total_mib": int(float(parts[-3])), "memory_used_mib": int(float(parts[-2])),
                             "driver": parts[-1], "compute_cap": None})
        except ValueError:
            continue
    return gpus


def select_gpu(gpus: list[dict], environ=None) -> dict | None:
    """判定と生成に使う GPU（点検 C32）。利用者が CUDA_VISIBLE_DEVICES を設定していればその 1 台目（UUID か番号。
    番号は PCI バスの順（nvidia-smi の番号と同じ。pci_bus_id が分からない古い nvidia-smi では出力の順）で数える。CUDA の既定の
    順（FASTEST_FIRST、速い順）とは違いうるので、GPU が 2 台以上で CUDA_DEVICE_ORDER=PCI_BUS_ID でなければ、:func:`gpu_items`
    が CUDA 自身に 0 番の GPU を尋ねて（:func:`cuda_device0_uuid`）こちらの推定を置き換える。点検 X3X-01・D2_backend-04）、
    設定が無ければ GPU メモリが最も大きいもの（同じなら nvidia-smi の順で先のもの）。指定に合う GPU が無ければ None。"""
    env = os.environ if environ is None else environ
    if not gpus:
        return None
    cvd = env.get("CUDA_VISIBLE_DEVICES")
    if cvd is None:
        return max(gpus, key=lambda g: (g["memory_total_mib"], -gpus.index(g)))
    first = cvd.split(",")[0].strip()
    if first.upper().startswith(("GPU-", "MIG-")):
        hit = [g for g in gpus if g.get("uuid") and g["uuid"].lower().startswith(first.lower())]
        if hit:
            return hit[0]
        return gpus[0] if first.upper().startswith("MIG-") else None     # MIG の区画は nvidia-smi の一覧と照らせない
    if first.isdigit():
        order = gpus
        if all(g.get("pci_bus_id") for g in gpus):
            order = sorted(gpus, key=lambda g: g["pci_bus_id"].lower())
        n = int(first)
        return order[n] if n < len(order) else None
    return None          # 空・-1 など（CUDA から GPU が見えない）


def gpu_env(info: dict | None = None, environ=None) -> dict:
    """TRELLIS の子プロセス（生成・セットアップの動作確認）に渡す、使う GPU の指定（点検 C32）。

    NVIDIA の GPU が 2 台以上あるとき、判定に使った GPU（:func:`select_gpu`）の UUID を CUDA_VISIBLE_DEVICES で渡し、
    CUDA の 0 番を同じ GPU にする（nvidia-smi の 1 台目と CUDA の 0 番は違いうる）。利用者が CUDA_VISIBLE_DEVICES を
    設定していればその指定に従い、何も足さない（番号の指定も CUDA の既定の数え方のまま。PC の他の CUDA アプリと同じ GPU を
    使う。判定の側が CUDA に 0 番の GPU を尋ねてそろえる。点検 D2_backend-04。以前の CUDA_DEVICE_ORDER=PCI_BUS_ID を足す
    形は、速い順で番号を決めた利用者の GPU を変えていた）。GPU が 1 台・UUID が分からないときは足さない。"""
    env = os.environ if environ is None else environ
    if env.get("CUDA_VISIBLE_DEVICES") is not None:
        return {}
    info = gpu_info() if info is None else info
    gpus = (info or {}).get("gpus") or []
    g = select_gpu(gpus, env)
    if g is None or len(gpus) < 2 or not g.get("uuid"):
        return {}
    return {"CUDA_VISIBLE_DEVICES": g["uuid"], "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}


# CUDA の 0 番の GPU を CUDA のドライバー（nvcuda.dll）に尋ねる子プロセス（点検 D2_backend-04）。cuInit・cuDeviceGet・
# cuDeviceGetUuid だけで、コンテキストも GPU メモリも作らない。UUID を nvidia-smi と同じ GPU-xxxxxxxx-… の形で出す
_CUDA0_CODE = r"""
import ctypes, sys
try:
    lib = ctypes.WinDLL("nvcuda.dll") if sys.platform == "win32" else ctypes.CDLL("libcuda.so.1")
except OSError:
    sys.exit(3)
if lib.cuInit(0) != 0:
    sys.exit(4)
dev = ctypes.c_int()
if lib.cuDeviceGet(ctypes.byref(dev), 0) != 0:
    sys.exit(5)
buf = (ctypes.c_ubyte * 16)()
f = getattr(lib, "cuDeviceGetUuid_v2", None) or getattr(lib, "cuDeviceGetUuid", None)
if f is None or f(buf, dev) != 0:
    sys.exit(6)
h = bytes(buf).hex()
print("GPU-%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:]))
"""
_CUDA0_RE = re.compile(r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
# 子プロセスを待つ秒数（cuInit は普通 1 秒ほど。環境チェックの要求を長く止めない。2026-11-04 退行の確認 2 回目 B3）
CUDA_QUERY_TIMEOUT = 10
# 尋ねられなかった（時間切れ・cuInit の失敗など）結果を覚えておく秒数。1 回の確認で何度も尋ねないためだけに短く覚え、
# 一時的な失敗をバックエンドの終わりまで残さない（B3）
CUDA_QUERY_RETRY_SECONDS = 60
# (CUDA_VISIBLE_DEVICES, CUDA_DEVICE_ORDER, GPU の UUID の並び) → (尋ねた結果（None を含む）, 尋ねた時刻 time.monotonic())。
# GPU の並びは再起動まで変わらないので、分かった結果は覚えたままにし、環境チェック・生成の事前確認のたびに子プロセスを
# 起動しない
_CUDA0_CACHE: dict = {}


def cuda_device0_uuid(environ) -> str | None:
    """利用者の環境変数のままの短い子プロセスで、CUDA の 0 番の GPU の UUID（nvidia-smi と同じ GPU-xxxxxxxx-… の形。16 進は小文字）を CUDA に尋ねる。
    生成・動作確認の子プロセス（:func:`gpu_env` は番号の指定に何も足さない）と同じ数え方になる。分からなければ None
    （ドライバーの API が無い・GPU が見えない・時間切れ）。"""
    try:
        r = subprocess.run([sys.executable, "-I", "-c", _CUDA0_CODE], env=dict(environ), capture_output=True, text=True,
                           errors="replace", timeout=CUDA_QUERY_TIMEOUT, stdin=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    out = "GPU-" + (r.stdout or "").strip()[4:].lower()
    return out if r.returncode == 0 and (r.stdout or "").strip()[:4].upper() == "GPU-" and _CUDA0_RE.match(out) else None


def _cuda0_gpu(gpus: list[dict], env) -> dict | None:
    """CUDA の 0 番の GPU（:func:`cuda_device0_uuid` の UUID に合う nvidia-smi の GPU）。分からなければ None。"""
    key = (env.get("CUDA_VISIBLE_DEVICES"), env.get("CUDA_DEVICE_ORDER"), tuple(str(g.get("uuid")) for g in gpus))
    got = _CUDA0_CACHE.get(key)
    if got is None or (got[0] is None and time.monotonic() - got[1] >= CUDA_QUERY_RETRY_SECONDS):
        got = _CUDA0_CACHE[key] = (cuda_device0_uuid(env), time.monotonic())
    uuid = got[0]
    hit = [g for g in gpus if uuid and g.get("uuid") and g["uuid"].lower() == uuid.lower()]
    return hit[0] if hit else None


def _unsure_note() -> str:
    return tr("（判定した GPU は推定なので止めません。生成に使う GPU とは違うことがあります）",
              " (the GPU checked is an estimate, so this does not stop generation; the GPU used for generation may differ)")


def vram_status(total_mib: int) -> str:
    """GPU メモリ（nvidia-smi の memory.total、MiB）→ ok / warning（12〜16 GB 級）/ error（12 GB 級未満）。"""
    return "ok" if total_mib >= VRAM_REC_MIB else ("warning" if total_mib >= VRAM_MIN_MIB else "error")


def gpu_items(info: dict | None, environ=None) -> list[CheckItem]:
    """GPU の判定（環境チェックと生成の事前確認で共通。点検 C03・C32・C127）→ 項目 gpu・gpu_driver・gpu_compute・gpu_memory。

    gpu 項目は ok か missing だけ（画面は gpu の ok だけを GPU ありとして扱う。点検 D2_backend-01）。番号の
    CUDA_VISIBLE_DEVICES がどの GPU かを CUDA に確かめられないときは、gpu 項目の文に注意を足し params.order_unknown を
    True にする（点検 D2_backend-04）。
    3D 生成を使えるのは、どの項目も ok か warning のとき。止める項目のコードと params.reason は、生成の事前確認の
    エラーにもそのまま使う（ドライバーが古い: GPU_NOT_FOUND / driver_old、compute capability が低い: GPU_NOT_FOUND /
    compute_capability、GPU メモリ不足: GPU_MEMORY_LOW）。"""
    env = os.environ if environ is None else environ
    gpus = (info or {}).get("gpus") or []
    if not gpus:
        return [CheckItem("gpu", "missing", "GPU_NOT_FOUND", tr("NVIDIA の GPU が見つかりません（nvidia-smi）",
                                                                "No NVIDIA GPU found (nvidia-smi)"),
                          {"error": (info or {}).get("error")})]
    cvd = env.get("CUDA_VISIBLE_DEVICES")
    g = select_gpu(gpus, env)
    order = (env.get("CUDA_DEVICE_ORDER") or "").strip().upper()
    # 番号の指定は CUDA の数え方（既定は速い順。CUDA_DEVICE_ORDER）で決まり、nvidia-smi の PCI バスの順とは違いうる。
    # 生成の子プロセスは利用者の指定のまま動くので、CUDA 自身に 0 番の GPU を尋ねて判定をそろえる。尋ねられなければ
    # 判定は PCI バスの順の推定のまま、注意（gpu 項目の文）を出して GPU の不足では止めない（点検 X3X-01・D2_backend-04）
    unsure = False
    if len(gpus) > 1 and cvd is not None and cvd.split(",")[0].strip().isdigit() and order != "PCI_BUS_ID":
        hit = _cuda0_gpu(gpus, env)
        if hit is not None:
            g = hit
        else:
            unsure = g is not None
    if g is None:
        return [CheckItem("gpu", "missing", "GPU_NOT_FOUND",
                          tr(f"環境変数 CUDA_VISIBLE_DEVICES（{cvd}）に合う NVIDIA の GPU がありません",
                             f"No NVIDIA GPU matches the CUDA_VISIBLE_DEVICES environment variable ({cvd})"),
                          {"reason": "cuda_visible_devices", "cuda_visible_devices": cvd, "gpus": [x["name"] for x in gpus]})]
    which = ""
    if len(gpus) > 1:
        which = (tr(f"（{len(gpus)} 台のうち、環境変数 CUDA_VISIBLE_DEVICES の指定の GPU を使います）",
                    f" (of {len(gpus)} GPUs, the one set in the CUDA_VISIBLE_DEVICES environment variable is used)")
                 if cvd is not None else
                 tr(f"（{len(gpus)} 台のうち、GPU メモリが最も大きいものを使います）",
                    f" (of {len(gpus)} GPUs, the one with the most GPU memory is used)"))
    # gpu 項目は ok か missing だけにする（画面の GPU の表示・初回セットアップの開始の判定と check_all の生成の判定は、
    # gpu の ok だけを GPU ありとして扱う。点検 D2_backend-01）。番号の GPU を CUDA に確かめられないときの注意は、gpu 項目の
    # 文に足し（画面の GPU の行と設定画面の環境チェックにそのまま出る）、params.order_unknown で示す（生成の事前確認は
    # 警告に足す）
    params = {"name": g["name"], "driver": g["driver"], "index": g.get("index"), "uuid": g.get("uuid"),
              "count": len(gpus), "gpus": [x["name"] for x in gpus]}
    note = ""
    if unsure:
        how = (tr(f"環境変数 CUDA_DEVICE_ORDER（{order}）の順", f"the order set in the CUDA_DEVICE_ORDER environment variable ({order})")
               if order else tr("CUDA の既定の順（速い順）", "CUDA's default order (fastest first)"))
        note = (tr(f"。注意: 環境変数 CUDA_VISIBLE_DEVICES（{cvd}）の番号は、{how}で数えます。どの GPU かを CUDA に確かめられ"
                   f"なかったので、ここで判定した GPU（nvidia-smi の番号で数えた推定）と生成に使う GPU が違うことがあります。"
                   f"GPU の不足では止めません（GPU を UUID で指定すると確かめられます）",
                   f". Note: the number in the CUDA_VISIBLE_DEVICES environment variable ({cvd}) is counted in {how}. "
                   f"It could not be confirmed with CUDA which GPU that is, so the GPU used for generation may differ "
                   f"from the one checked here (estimated by the nvidia-smi numbering). A lack of GPU capability does "
                   f"not stop generation (specify the GPU by its UUID to have it checked)"))
        params.update({"order_unknown": True, "reason": "cuda_order_unknown", "cuda_visible_devices": cvd,
                       "cuda_device_order": order or None})
    items = [CheckItem("gpu", "ok", None, tr(f"{g['name']}（ドライバー {g['driver']}）{which}",
                                             f"{g['name']} (driver {g['driver']}){which}") + note, params)]
    # CUDA 12.8 の PyTorch には R570 以降のドライバーが必要（setup_trellis_env.ps1 と同じ判定）
    try:
        major = int(str(g["driver"]).split(".")[0])
    except ValueError:
        major = 0
    dst = "ok" if major >= MIN_DRIVER_MAJOR else "error"
    items.append(CheckItem("gpu_driver", dst, None if dst == "ok" else "GPU_NOT_FOUND",
                           tr(f"ドライバー {g['driver']}", f"Driver {g['driver']}")
                           + ("" if dst == "ok" else tr(f"（{MIN_DRIVER_MAJOR} 以降が必要）",
                                                        f" ({MIN_DRIVER_MAJOR} or later is required)")),
                           {"driver": g["driver"], "required_major": MIN_DRIVER_MAJOR,
                            **({} if dst == "ok" else {"reason": "driver_old"})}))
    cap = g.get("compute_cap")
    if cap:
        cv = tuple(int(x) for x in cap.split("."))
        cst = "error" if cv < MIN_COMPUTE_CAP else ("warning" if cv < WARN_COMPUTE_CAP else "ok")
        msg = {"ok": f"compute capability {cap}",
               "warning": tr(f"compute capability {cap}（7.0 未満の GPU での生成は確かめていません。遅くなるか失敗することがあります）",
                             f"compute capability {cap} (generation on GPUs below 7.0 has not been tested; it may be slow or fail)"),
               "error": tr(f"compute capability {cap}（6.0 以上が必要です）",
                           f"compute capability {cap} (6.0 or higher is required)")}[cst]
        reason = {"reason": "compute_capability"} if cst == "error" else {}
        if cst == "error" and unsure:           # 推定した GPU の不足では止めない（gpu 項目の注意。D2_backend-04）
            cst = "warning"
            msg += _unsure_note()
        items.append(CheckItem("gpu_compute", cst, "GPU_NOT_FOUND" if cst == "error" else None, msg,
                               {"compute_cap": cap, "required": "6.0", **reason}))
    total = g["memory_total_mib"]
    st = vram_status(total)
    msg = tr(f"GPU メモリ {total} MiB（使用中 {g['memory_used_mib']} MiB）",
             f"GPU memory {total} MiB ({g['memory_used_mib']} MiB in use)")
    if st == "warning":
        msg += tr("。16 GB 未満のため、メモリ不足で失敗することがあります（TRELLIS の README は 16 GB 以上を必須としています）",
                  ". It is less than 16 GB, so generation may fail for lack of memory (the TRELLIS README requires 16 GB or more)")
    elif st == "error":
        msg += tr("。3D 生成には 12 GB 以上が必要です", ". 3D generation requires 12 GB or more")
        if unsure:                              # 推定した GPU の不足では止めない（gpu 項目の注意。D2_backend-04）
            st = "warning"
            msg += _unsure_note()
    items.append(CheckItem("gpu_memory", st, None if st == "ok" else "GPU_MEMORY_LOW", msg,
                           {"total_mib": total, "used_mib": g["memory_used_mib"], "required_mib": VRAM_MIN_MIB,
                            "recommended_mib": VRAM_REC_MIB}))
    return items


def file_version(path: str) -> tuple[int, int, int, int] | None:
    """Windows のファイルの版（VS_FIXEDFILEINFO の FileVersion）。読めなければ None。"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [("dwSignature", wintypes.DWORD), ("dwStrucVersion", wintypes.DWORD),
                    ("dwFileVersionMS", wintypes.DWORD), ("dwFileVersionLS", wintypes.DWORD),
                    ("dwProductVersionMS", wintypes.DWORD), ("dwProductVersionLS", wintypes.DWORD),
                    ("dwFileFlagsMask", wintypes.DWORD), ("dwFileFlags", wintypes.DWORD), ("dwFileOS", wintypes.DWORD),
                    ("dwFileType", wintypes.DWORD), ("dwFileSubtype", wintypes.DWORD),
                    ("dwFileDateMS", wintypes.DWORD), ("dwFileDateLS", wintypes.DWORD)]
    try:
        ver = ctypes.WinDLL("version")
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        ver.GetFileVersionInfoW.restype = wintypes.BOOL
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
        ver.VerQueryValueW.restype = wintypes.BOOL
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None
        p = ctypes.c_void_p()
        n = wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(n)) or n.value < ctypes.sizeof(VS_FIXEDFILEINFO):
            return None
        fi = ctypes.cast(p, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        return (fi.dwFileVersionMS >> 16, fi.dwFileVersionMS & 0xFFFF, fi.dwFileVersionLS >> 16, fi.dwFileVersionLS & 0xFFFF)
    except (OSError, AttributeError, ValueError):
        return None


def _min_vc_runtime() -> tuple[int, int]:
    try:
        from . import setup as _setup
        text = str(_setup.manifest().get("min_vc_runtime") or VC_RUNTIME_MIN_DEFAULT)
    except Exception:   # noqa: BLE001  セットアップの定義が読めない（配布物の破損など）
        text = VC_RUNTIME_MIN_DEFAULT
    major, minor = (int(x) for x in text.split(".")[:2])
    return major, minor


def vc_runtime_item(system32: str | None = None, required: tuple[int, int] | None = None,
                    version_of=file_version) -> CheckItem | None:
    """Visual C++ 再頒布可能パッケージ（x64）の版（点検 C31）。System32 の msvcp140.dll の版が最小値（setup_manifest.json の
    min_vc_runtime）以上で、msvcp140_1.dll・vcomp140.dll もあれば ok、無い・古い・版が読めなければ warning。
    vcruntime140*.dll は TRELLIS 実行環境の Python が持つので見ない。3D 生成だけに関わるので、can_* の判定には入れない。"""
    if system32 is None:
        if sys.platform != "win32":
            return None
        system32 = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32")
    req = required or _min_vc_runtime()
    missing = [n for n in VC_RUNTIME_DLLS if not os.path.isfile(os.path.join(system32, n))]
    v = None if "msvcp140.dll" in missing else version_of(os.path.join(system32, "msvcp140.dll"))
    vtext = ".".join(map(str, v)) if v else None
    params = {"version": vtext, "required": f"{req[0]}.{req[1]}", "missing": missing, "url": VC_REDIST_URL}
    if not missing and v is not None and v[:2] >= req:
        return CheckItem("vc_runtime", "ok", None, tr(f"Visual C++ 再頒布可能パッケージ（x64）: {vtext}",
                                                      f"Visual C++ Redistributable (x64): {vtext}"), params)
    if missing:
        why = tr(f"{', '.join(missing)} がありません", f"{', '.join(missing)} not found")
    elif v is None:
        why = tr("版を確かめられません", "the version cannot be checked")
    else:
        why = tr(f"版 {vtext} が古いです", f"version {vtext} is too old")
    return CheckItem("vc_runtime", "warning", None,
                     tr(f"Visual C++ 再頒布可能パッケージ（x64）: {why}（{req[0]}.{req[1]} 以降が必要）。3D 生成を使う前に、"
                        f"最新版（{VC_REDIST_URL}）を入れてください（管理者権限が要ります）。GLB の変換と MOD の作成には影響しません",
                        f"Visual C++ Redistributable (x64): {why} ({req[0]}.{req[1]} or later is required). Before using 3D "
                        f"generation, install the latest version ({VC_REDIST_URL}; administrator rights are required). GLB "
                        "conversion and MOD builds are not affected"),
                     params)


def os_name() -> str:
    """Windows の名前（Python 3.11 の platform.release() は Windows 11 でも "10" を返すので、ビルド番号で判定する）。"""
    if sys.platform != "win32":
        return f"{platform.system()} {platform.release()} {platform.machine()}"
    ver = platform.version()          # 例 10.0.26300
    try:
        build = int(ver.split(".")[2])
    except (IndexError, ValueError):
        build = 0
    name = "Windows 11" if build >= 22000 else f"Windows {platform.release()}"
    return tr(f"{name}（ビルド {build}、{platform.machine()}）", f"{name} (build {build}, {platform.machine()})")


def game_running() -> bool | None:
    """Grim Dawn が起動しているか。判定できなければ None（2026-10-17 監査 C90。呼び出し側は None を「起動中ではない」と
    見なさず、導入の計画に注意を出す。``if game_running():`` の使い方では従来どおり止めない）。"""
    try:
        from modbuilder import steam
        r = steam.game_running()
    except Exception:   # noqa: BLE001
        r = None
    if r is None:
        return True if process_running(GAME_PROCESSES) else None
    return bool(r)


def work_dir_item(home: str) -> CheckItem:
    """ArchiveTool の -database の出力先に使う作業フォルダー（modbuilder.arc.safe_work_dir と同じ候補・同じ判定。
    ここではフォルダーを作らない）。どれも使えなければ、MOD の作成のときに ArchiveTool の照合（V2）を省く。"""
    from modbuilder.arc import safe_work_dir, work_dir_candidates
    try:
        d, skipped = safe_work_dir(home, create=False)
    except Exception as e:   # noqa: BLE001
        return CheckItem("work_dir", "warning", None, tr(f"作業用の一時フォルダーを確かめられません: {e}",
                                                         f"Cannot check the temporary working folder: {e}"))
    first = work_dir_candidates(home)[0]
    params = {"path": d, "skipped": [{"path": c, "reason": why} for c, why in skipped]}
    if d is None:
        return CheckItem("work_dir", "warning", None,
                         tr("一時フォルダー・データフォルダーのパスに、ゲーム付属の ArchiveTool が扱えない文字があるか、パスが長すぎます。"
                            "MOD の作成のとき、ArchiveTool による照合を省きます（自前の読み取りでの照合は行います）",
                            "The paths of the temporary folder and the data folder contain characters that the game's bundled "
                            "ArchiveTool cannot handle, or are too long. When building the MOD, the check with ArchiveTool is "
                            "skipped (the check with the app's own reader is still done)"), params)
    if os.path.normcase(d) == os.path.normcase(first):
        return CheckItem("work_dir", "ok", None, d, params)
    if dict(skipped).get(first) == "too_long":
        msg = tr(f"{d}（一時フォルダーのパスが長すぎるため、こちらを使います）",
                 f"{d} (used because the temporary folder path is too long)")
    else:
        msg = tr(f"{d}（一時フォルダーのパスに ArchiveTool が扱えない文字があるため、こちらを使います）",
                 f"{d} (used because the temporary folder path contains characters that ArchiveTool cannot handle)")
    return CheckItem("work_dir", "ok", None, msg, params)


# データフォルダーの中でアプリが作る最も深いファイルの相対パスの長さの見込み（2026-10-17 機能 SF22。測定:
# builds\<ID>\staging\database\records\creatures\npcs\merchants\factiontables\devilscrossing_respected_01.dbr が 118 文字。
# ゲーム付属のツールに渡す最も深いものは builds\<ID>\staging\resources\Items\aiskin\<装備ID>\<装備ID>_dif.tex の 87 文字）
DATA_DEPTH = 120


def data_path_item(home: str, temp: str | None = None, trellis_root: str | None = None) -> CheckItem:
    """データフォルダー・一時フォルダー・TRELLIS 実行環境のパスの安全性（2026-10-17 機能 SF22。読み取りのみで、フォルダーは
    作らない。判定は C01・C02・C16 と同じ modbuilder.arc.path_problem と、Windows のパスの長さの上限）。

    * データフォルダー: 中に作るファイル（:data:`DATA_DEPTH`）が上限を超える長さなら warning（ゲーム付属のツールは長いパスを
      扱えず、出力が終了コード 0 のまま欠けることがある）。ツールが扱えない文字は知らせるだけ（ツールには相対パスで渡し、
      ArchiveTool の出力先は作業用の一時フォルダーを選ぶので、今は失敗しない）。
    * 一時フォルダー（%TEMP%）: 同じ判定（長さは ArchiveTool の出力の深さ WORK_DEPTH）。長すぎれば warning（MOD の作成の
      照合は別のフォルダーを使うが、ほかの処理が使う）。
    * TRELLIS 実行環境（見つかったときだけ）: セットアップの取得先の規則（setup.check_root の too_long・non_ascii）に
      外れれば warning。"""
    from modbuilder.arc import MAX_PATH_CHARS, WORK_DEPTH, path_problem, path_too_long
    temp = temp or tempfile.gettempdir()
    locs, warns, notes = [], [], []
    for kind, p, depth in (("data", home, DATA_DEPTH), ("temp", temp, WORK_DEPTH)):
        p = os.path.abspath(p)
        prob = path_problem(p)
        too_long = path_too_long(p, depth)
        limit = MAX_PATH_CHARS - 1 - depth
        locs.append({"kind": kind, "path": p, "problem": prob, "length": len(p), "max_length": limit, "too_long": too_long})
        if too_long:
            if kind == "data":
                warns.append(tr(f"データフォルダーのパスが長すぎます（{len(p)} 文字。{limit} 文字まで）。中に作るファイルのパスが Windows の"
                                "上限（260 文字）を超え、保存や MOD の作成に失敗することがあります。短いフォルダー（例 C:\\GDSkinData）を"
                                " GDSkinStudio.exe の起動引数 --data-dir で指定してください",
                                f"The data folder path is too long ({len(p)} characters; up to {limit}). The paths of files "
                                "created in it exceed the Windows limit (260 characters), so saving or building the MOD may "
                                "fail. Specify a short folder (for example C:\\GDSkinData) with the --data-dir option of GDSkinStudio.exe"))
            else:
                warns.append(tr(f"一時フォルダーのパスが長すぎます（{len(p)} 文字。{limit} 文字まで）。MOD の作成の照合には別のフォルダーを"
                                "使いますが、ほかの処理が失敗することがあります。環境変数 TEMP・TMP を短いフォルダーにしてください",
                                f"The temporary folder path is too long ({len(p)} characters; up to {limit}). Another folder "
                                "is used for the check when building the MOD, but other processing may fail. Set the TEMP and "
                                "TMP environment variables to a short folder"))
        if prob:
            if kind == "data":
                notes.append(tr("データフォルダーのパスに、ゲーム付属のツールがそのままでは扱えない文字があります（ツールには回避して渡します）",
                                "The data folder path contains characters that the game's bundled tools cannot handle as is "
                                "(the paths are passed to the tools in a way that avoids them)"))
            else:
                notes.append(tr("一時フォルダーのパスに、ゲーム付属のツールがそのままでは扱えない文字があります（MOD の作成の照合には別の"
                                "フォルダーを使います）",
                                "The temporary folder path contains characters that the game's bundled tools cannot handle as "
                                "is (another folder is used for the check when building the MOD)"))
    trellis = None
    if trellis_root:
        from .setup import check_root
        try:
            probs = [x for x in check_root(trellis_root) if x["code"] in ("too_long", "non_ascii")]
        except Exception:   # noqa: BLE001
            probs = []
        trellis = {"path": trellis_root, "problems": [x["code"] for x in probs]}
        if probs:
            found = tr("・", " / ").join(x["message"] for x in probs)
            warns.append(tr(f"TRELLIS 実行環境: {found}。3D 生成に失敗するときは、セットアップで半角の英数字・記号だけの短いフォルダーに取得し直してください",
                            f"TRELLIS runtime: {found}. If 3D generation fails, download it again in Setup to a short folder "
                            "whose path uses only ASCII letters, digits and symbols"))
    params = {"locations": locs, "trellis": trellis}
    if not warns and not notes:
        if trellis:
            msg = tr("データフォルダー・一時フォルダー・TRELLIS 実行環境のパスに問題はありません",
                     "No problems with the paths of the data folder, the temporary folder and the TRELLIS runtime")
        else:
            msg = tr("データフォルダー・一時フォルダーのパスに問題はありません",
                     "No problems with the paths of the data folder and the temporary folder")
        return CheckItem("data_path", "ok", None, msg, params)
    return CheckItem("data_path", "warning" if warns else "ok", None, tr("。", ". ").join(warns + notes), params)


def _has_basemods(o: dict) -> bool:
    return "/basemods" in (o.get("options") or "").lower()


def launch_option_state(root: str | None = None, current=None, running=None) -> dict:
    """Steam の起動オプションの /basemods の有無（ユーザーIDは返さない。読み取りのみ。D-022）。

    今の Steam ユーザー（:func:`modbuilder.steam.current_user`）が分かれば、その人の設定で判定する（scope
    ``current_user``、state ``all``（ある）/ ``none``（無い））。このPCの他の Steam ユーザーの未設定は ``others_without``
    に数えるだけで、警告にしない（家族の別アカウントで 'some' の警告になっていた。2026-10-17 機能 SF12）。分からなければ
    従来どおり全ユーザーで判定する（scope ``all_users``、all / some / none）。設定のファイルが無ければ unknown。
    ``detected_by``: active_user（ログイン中のユーザー）/ last_active_user（Steam が起動していないときに HKCU に残っている
    最後のユーザー）/ login_users（loginusers.vdf の最後のユーザー）/ None。active_user 以外は「最後に Steam にログインした
    ユーザー」と表示する（launch_option_message・画面の LaunchInfo）。
    ``steam_running``: Steam が起動中か（None は判定できない。起動中に設定した直後はファイルに書かれていないことがある）。
    root・current（(ID, 方法)）・running はテスト用。"""
    base = {"state": "unknown", "users": 0, "users_with_basemods": 0, "scope": "all_users", "detected_by": None,
            "others_without": 0, "steam_running": running}
    try:
        from modbuilder import steam
        root = root or steam.steam_root()
        opts = steam.launch_options(root)
    except Exception:   # noqa: BLE001
        return base
    if running is None:
        try:
            base["steam_running"] = steam.steam_running()
        except Exception:   # noqa: BLE001
            base["steam_running"] = None
    if not opts:
        return base
    has = [o for o in opts if _has_basemods(o)]
    base.update(users=len(opts), users_with_basemods=len(has))
    if current is None:
        try:
            current = steam.current_user(root)
        except Exception:   # noqa: BLE001
            current = (None, None)
    uid, how = current or (None, None)
    if uid is not None:
        mine = [o for o in opts if str(o.get("user")) == str(uid)]
        ok = bool(mine) and _has_basemods(mine[0])
        base.update(state="all" if ok else "none", scope="current_user", detected_by=how,
                    others_without=sum(1 for o in opts if str(o.get("user")) != str(uid) and not _has_basemods(o)))
        return base
    base["state"] = "all" if len(has) == len(opts) else ("some" if has else "none")
    return base


def launch_option_message(lo: dict) -> str:
    """環境チェックの launch_option の項目の文。"""
    st = lo.get("state")
    if lo.get("scope") == "current_user":
        who = (tr("今 Steam にログインしているユーザー", "the user currently signed in to Steam")
               if lo.get("detected_by") == "active_user" else
               tr("最後に Steam にログインしたユーザー", "the user who last signed in to Steam"))
        msg = (tr(f"{who}の起動オプションに /basemods があります", f"/basemods is set in the launch options of {who}") if st == "all"
               else tr(f"{who}の起動オプションに /basemods がありません", f"/basemods is not set in the launch options of {who}"))
        if lo.get("others_without"):
            msg += tr(f"（この PC の他の Steam ユーザー {lo['others_without']} 人は未設定です）",
                      f" (not set for {lo['others_without']} other Steam user{'' if lo['others_without'] == 1 else 's'} "
                      "on this PC)")
    else:
        msg = {"all": tr("Steam の起動オプションに /basemods があります", "/basemods is set in the Steam launch options"),
               "some": tr(f"一部の Steam ユーザーだけに /basemods があります（{lo.get('users_with_basemods', 0)}/{lo.get('users', 0)} 人）",
                          f"/basemods is set for only some Steam users ({lo.get('users_with_basemods', 0)}/{lo.get('users', 0)})"),
               "none": tr("Steam の起動オプションに /basemods がありません", "/basemods was not found in the Steam launch options")}.get(
                   st, tr("Steam の起動オプションを確認できません", "Could not check the Steam launch options"))
    if st != "all" and lo.get("steam_running") is not False:
        msg += tr("。Steam の起動中に設定した直後は、Steam を終了すると設定がファイルに書かれ、確認できるようになります",
                  ". If you have just set it while Steam is running, it can be checked after you exit Steam (Steam writes "
                  "the setting to its files when it exits)")
    return msg


def refs_problems(refs_dir: str) -> tuple[list[str], list[str], str | None]:
    """あるが基準として使えない基準ファイル → (壊れている, 開けない・読めない, 開けない理由)。ゲーム内パスの一覧。

    開けない（ほかのアプリが共有なしで開いている・アクセス権が無い）OSError も例外にしない（環境チェックの全体・生成の
    事前確認を想定外のエラーにしない。点検 D2_backend-03）。gdconvert.refs に理由付きの ref_problems があれば使い、
    無ければ unreadable_refs の OSError を受ける。理由はパスを含めない（例外の型と番号だけ）。"""
    import gdconvert.refs as refs
    try:
        probs = getattr(refs, "ref_problems", None)
        if probs is not None:
            got = probs(refs_dir)
            return ([k for k, v in got.items() if v != "unreadable"], [k for k, v in got.items() if v == "unreadable"],
                    None)
        return refs.unreadable_refs(refs_dir), [], None
    except OSError as e:
        rel = os.path.relpath(e.filename, refs_dir).replace(os.sep, "/") if isinstance(e.filename, str) else None
        if rel is not None and rel.startswith(".."):
            rel = None
        return [], [rel or "?"], f"{type(e).__name__} (errno {e.errno})"


def check_all(svc) -> dict:
    from gdconvert.refs import missing_refs
    s = svc.settings.get()
    items: list[CheckItem] = []
    # OS
    win = sys.platform == "win32"
    items.append(CheckItem("os", "ok" if win and platform.machine().endswith("64") else "error",
                           None if win else "INTERNAL", os_name()))
    # GPU（gpu・gpu_driver・gpu_compute・gpu_memory。生成の事前確認と同じ判定）
    gpu_ids = []
    for c in gpu_items(gpu_info()):
        gpu_ids.append(c.id)
        items.append(c)
    # Visual C++ 再頒布可能パッケージ（3D 生成の TRELLIS 実行環境が使う。点検 C31）
    vc = vc_runtime_item()
    if vc is not None:
        items.append(vc)
    # バックエンドの部品
    miss = [pkg for mod, pkg in BACKEND_MODULES.items() if importlib.util.find_spec(mod) is None]
    items.append(CheckItem("backend_modules", "ok" if not miss else "missing", None if not miss else "LIBRARY_MISSING",
                           tr("変換・MOD 作成の部品: すべてあり", "Conversion and MOD build components: all present") if not miss
                           else tr(f"変換・MOD 作成の部品: 不足 {', '.join(miss)}",
                                   f"Conversion and MOD build components: missing {', '.join(miss)}"),
                           {"missing": miss, "python": sys.version.split()[0]}))
    # 3D 生成 AI
    for prov in providers.REGISTRY.values():
        for c in prov.check(s):
            c.id = f"{prov.id}.{c.id}"
            items.append(c)
    # Grim Dawn（ゲームの判定は database\database.arz だけ。付属のツールは個別の項目にして、欠けているファイルを示す。
    # 2026-10-17 点検 C82）
    gd = resolve_game_dir(s)
    if gd is None:
        items.append(CheckItem("game", "missing", "GAME_NOT_FOUND", tr("Grim Dawn のインストール先が見つかりません",
                                                                       "The Grim Dawn install folder was not found"),
                               {"configured": s.get("game_dir") or None}))
    else:
        items.append(CheckItem("game", "ok", None, gd, {"path": gd}))
        from modbuilder.gamedata import GAME_TOOLS
        for exe in GAME_TOOLS:
            ok = os.path.isfile(os.path.join(gd, exe))
            items.append(CheckItem(f"game_{exe.split('.')[0].lower()}", "ok" if ok else "missing",
                                   None if ok else "GAME_NOT_FOUND",
                                   tr(f"{exe}: あり", f"{exe}: found") if ok else
                                   tr(f"{exe}: ありません（Steam のライブラリで Grim Dawn のプロパティ →"
                                      "「インストール済みファイル」→「ゲームファイルの整合性を確認」で戻せます）",
                                      f"{exe}: not found (you can restore it in the Steam library: Grim Dawn's Properties → "
                                      "\"Installed Files\" → \"Verify integrity of game files\")"),
                                   {"path": os.path.join(gd, exe), "present": ok}))
        # 拡張 Ashes of Malmouth（gdx1）: MOD の雛形のレコード（透明ヘルムなど）が gdx1 にある（modbuilder.records）
        aom = os.path.isfile(os.path.join(gd, "gdx1", "database", "GDX1.arz"))
        items.append(CheckItem("game_aom", "ok" if aom else "missing", None if aom else "GAME_NOT_FOUND",
                               tr("拡張 Ashes of Malmouth: あり", "Expansion Ashes of Malmouth: installed") if aom else
                               tr("拡張 Ashes of Malmouth: ありません（MOD の作成に必要）",
                                  "Expansion Ashes of Malmouth: not installed (required to build the MOD)"), {"present": aom}))
    # 基準メッシュ
    miss_refs = missing_refs(svc.refs_dir())
    # あるが読めない（切れた・0 で埋まった）基準ファイル。変換は REFS_MISSING で止まるので、ここでも分かるようにする
    # （3 ファイルを読んで数十ミリ秒。点検 R03A-01）。開けない（ほかのアプリが共有なしで開いている・アクセス権が無い）
    # ものも、例外にせず別の文で示す（点検 D2_backend-03）
    bad_refs, locked_refs, locked_error = ([], [], None) if miss_refs else refs_problems(svc.refs_dir())
    if miss_refs:
        refs_msg = tr(f"基準メッシュ: {len(miss_refs)} ファイル不足",
                      f"Reference meshes: {len(miss_refs)} file{'' if len(miss_refs) == 1 else 's'} missing")
    elif bad_refs:
        refs_msg = tr("基準メッシュ: 壊れている（「ゲームから準備する」で取り出し直す）",
                      "Reference meshes: corrupted (extract them again with \"Prepare from the game\")")
    elif locked_refs:
        refs_msg = tr("基準メッシュ: 開けない（ほかのアプリが開いていないか、読み取りの権限を確かめてから、もう一度確認する）",
                      "Reference meshes: cannot be opened (check that no other app has them open and that you have "
                      "permission to read them, then check again)")
    else:
        refs_msg = tr("基準メッシュ: あり", "Reference meshes: ready")
    refs_ok = not miss_refs and not bad_refs and not locked_refs
    refs_params = {"refs_dir": svc.refs_dir(), "missing": miss_refs, "corrupt": bad_refs}
    if locked_refs:
        refs_params.update({"reason": "refs_unreadable", "unreadable": locked_refs, "error": locked_error})
    if bad_refs:
        # 文（壊れている）と画面の文（Error_REFS_MISSING_refs_corrupt_*）をそろえる。開けない一覧は残す
        # （2026-11-04 退行の確認 2 回目 B1）
        refs_params["reason"] = "refs_corrupt"
    items.append(CheckItem("refs", "ok" if refs_ok else "missing", None if refs_ok else "REFS_MISSING", refs_msg,
                           refs_params))
    # ArchiveTool の作業フォルダー（MOD の作成の照合 V2 の -database の出力先。2026-10-17 利用者の判断 C02）。
    # 実際に ArchiveTool が壊すパス（modbuilder.arc.path_problem）だけを問題にする（佐藤・鈴木 などでは警告しない）
    items.append(work_dir_item(svc.home()))
    # データフォルダー・一時フォルダー・TRELLIS 実行環境のパスの安全性（2026-10-17 機能 SF22。警告だけで、ボタンの判定には入れない）
    try:
        items.append(data_path_item(svc.home(), trellis_root=resolve_trellis_root(s)))
    except Exception as e:   # noqa: BLE001
        items.append(CheckItem("data_path", "warning", None, tr(f"パスを確かめられません: {type(e).__name__}",
                                                                f"Cannot check the paths: {type(e).__name__}")))
    # 導入先
    target, is_test = resolve_install_target(s)
    from modbuilder.gamedata import looks_like_install_target
    if target and looks_like_install_target(target):
        items.append(CheckItem("install_target", "warning" if is_test else "ok", None,
                               target + (tr("（テスト用の導入先）", " (test install target)") if is_test else ""),
                               {"path": target, "test": is_test}))
    else:
        items.append(CheckItem("install_target", "missing", "GAME_NOT_FOUND",
                               tr("MOD の導入先が Grim Dawn のフォルダーではありません",
                                  "The MOD install target is not a Grim Dawn folder"),
                               {"path": target, "test": is_test}))
    # 起動オプション
    lo = launch_option_state()
    st = {"all": "ok", "some": "warning", "none": "warning", "unknown": "warning"}[lo["state"]]
    items.append(CheckItem("launch_option", st, None, launch_option_message(lo), lo))
    # ゲームの起動
    running = game_running()
    items.append(CheckItem("game_running", "ok" if running is False else "warning", "GAME_RUNNING" if running else None,
                           {True: tr("Grim Dawn が起動中です", "Grim Dawn is running"),
                            False: tr("Grim Dawn は起動していません", "Grim Dawn is not running")}.get(
                               running, tr("Grim Dawn が起動しているか確かめられません",
                                           "Could not check whether Grim Dawn is running")), {"running": running}))
    # データフォルダーが別のアプリの仮想フォルダーと分かれていないか（docs/app_architecture.md 12 章 #15）
    from . import datastores
    try:
        ds = datastores.scan(svc.library_dir(), svc.home())
    except Exception:   # noqa: BLE001
        ds = {"virtualized_into": None, "stores": []}
    if ds["virtualized_into"]:
        items.append(CheckItem("data_virtualized", "warning", None,
                               tr("このアプリは別のアプリ（Claude など）の中から起動されているため、新しく作るファイルは仮想フォルダーに入り、"
                                  "普通に起動したアプリからは見えません",
                                  "This app was started from inside another app (such as Claude), so new files are saved in a "
                                  "virtualized folder and cannot be seen by the app when started normally"),
                               {"store": ds["virtualized_into"]}))
    else:
        n = datastores.missing_count(ds)
        items.append(CheckItem("data_elsewhere", "warning" if n else "ok", None,
                               tr(f"別のアプリの仮想フォルダーに、このアプリから見えない装備が {n} 件あります（装備一覧から取り込めます）",
                                  f"{n} equipment in another app's virtualized folder cannot be seen by this app (you can "
                                  "import it from the Library)")
                               if n else tr("データフォルダーは 1 か所です", "There is only one data folder"),
                               {"missing": n, "stores": [s["path"] for s in ds["stores"] if s["missing"]]}))
    # 初回セットアップ（docs/packaging.md 4 章）
    from . import setup as _setup
    try:
        st = _setup.load_state()
        done = bool(st.get("completed_at"))
        items.append(CheckItem("setup", "ok" if done else "warning", None,
                               tr("初回セットアップ: 完了", "First-run setup: completed") if done else
                               tr("初回セットアップ: 後で行う", "First-run setup: postponed") if st.get("skipped_at") else
                               tr("初回セットアップ: 未実施", "First-run setup: not completed"),
                               {"completed_at": st.get("completed_at"), "skipped_at": st.get("skipped_at")}))
    except Exception:   # noqa: BLE001
        pass
    # 空き容量
    try:
        free = shutil.disk_usage(svc.home()).free
        items.append(CheckItem("disk_space", "ok" if free >= FREE_SPACE_WARN else "warning", None,
                               tr(f"空き {free / 1024 ** 3:.1f} GB", f"{free / 1024 ** 3:.1f} GB free"), {"free_bytes": free}))
    except OSError:
        pass

    by_id = {c.id: c for c in items}

    def ok(*ids):
        return all(by_id.get(i) is not None and by_id[i].status in ("ok", "warning") for i in ids)

    # 3D 生成: GPU の項目（ドライバー・compute capability・GPU メモリを含む）がどれも ok か warning（点検 C32）。
    # gpu 項目そのものは ok か missing だけ（注意は別の項目。画面と同じく ok を GPU ありとする。点検 D2_backend-01）
    gen_ok = by_id.get("gpu") is not None and by_id["gpu"].status == "ok" and \
        all(by_id[i].status in ("ok", "warning") for i in gpu_ids)
    conv_ok = ok("backend_modules", "game", "refs") and by_id.get("game_texturecompiler", CheckItem("x", "missing")).status == "ok"
    summary = {
        "can_generate_image": gen_ok and conv_ok and all(
            c.status in ("ok", "warning") for c in items if c.id.startswith("trellis.") and not c.id.endswith(("_text", "_clip", "consent"))),
        "can_generate_text": gen_ok and conv_ok and all(
            c.status in ("ok", "warning") for c in items if c.id.startswith("trellis.") and not c.id.endswith("_image")),
        "can_convert": conv_ok,
        "can_build": ok("backend_modules", "game", "game_archivetool")
        and (by_id.get("game_aom") is None or by_id["game_aom"].status == "ok"),
        "can_install": ok("install_target"),
        "game_running": running,
    }
    return {"items": [c.to_dict() for c in items], "summary": summary}
