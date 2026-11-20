"""Parts of generate_glb.py that need neither torch nor a GPU (checked with pytest by the backend's Python).

* pinned_hub_load: read the pinned DINOv2 hub code from <TORCH_HOME>/hub with source='local' (D-025), so
  torch.hub never asks github.com and never fetches unpinned code
* check_model_files / dinov2_problems / local_only_from_pretrained: every model file is checked before loading,
  and a local file that fails to load is never replaced by a Hugging Face download
* env_warnings: environment variables that move the pinned caches or the attention backend
* normalize_image: the same input normalisation as the app (gdskin_backend.pipeline.copy_image)
* exclusions / choose_candidate / orientation_disagreement: candidate selection rules (D-012 / D-013)
* sampler_overrides / multi_image_steps: optional sampler steps and guidance strength (SF05; none = pipeline.json)
* block_network: optional strict offline mode (SF17; refuses connections that leave the machine and logs them)
* block_downloads: default mode (SF17 (2)); a missing model file is never downloaded during generation (rembg's
  pooch, torch.hub's weights), the run stops with a fixed message that points to the setup
"""
from __future__ import annotations

import hashlib
import importlib
import ipaddress
import json
import os

DINOV2_REPO = "facebookresearch/dinov2"
DINOV2_HUB_FOLDER = "facebookresearch_dinov2_main"   # torch.hub's folder for the repo's default branch
# fetch_models.py writes these (setup_manifest.json: dinov2), torch.hub.load_state_dict_from_url reads checkpoints/<name>
DINOV2_WEIGHTS_NAME = "dinov2_vitl14_reg4_pretrain.pth"

MISSING_MARKER = "ERROR: model files are missing or damaged"
NOT_LOADED_MARKER = "model file not loaded"
HUB_MISSING_MARKER = "DINOv2 hub code is missing"
EMPTY_MARKER = "ERROR: no object was generated"
# --from-dump: the saved sample (.pt) cannot be loaded or is not a sample (the app shows sample_unreadable)
SAMPLE_BAD_MARKER = "ERROR: the saved sample cannot be read"
ORIENTATION_ANGLE_TOL = 15.0   # degrees; as-generated vs turned 180 deg differ by at most 3 deg on ordinary bodies


# --- the app's display language (D-060) -------------------------------------------------------------------------
def lang() -> str:
    """The app's display language from GDSKIN_LANG ('en' / 'en-US' -> 'en', anything else -> 'ja'). The same rule as
    app/backend/gdskin_backend/i18n.py; the app's backend passes the variable to this process."""
    s = (os.environ.get("GDSKIN_LANG") or "").strip().lower()
    return "en" if s == "en" or s.startswith("en-") else "ja"


def tr(ja: str, en: str) -> str:
    """The message in the display language. The markers above stay in English in both (the app matches them)."""
    return en if lang() == "en" else ja


# --- DINOv2 from the pinned hub folder (D-025) -----------------------------------------------------------------
def dinov2_hub_dir(hub_dir: str) -> str:
    return os.path.join(hub_dir, DINOV2_HUB_FOLDER)


def read_pinned_commit(folder: str) -> str | None:
    try:
        with open(os.path.join(folder, "PINNED_COMMIT"), encoding="ascii") as f:
            return f.read().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def hub_missing_message(folder: str) -> str:
    return tr(f"{HUB_MISSING_MARKER}: {os.path.join(folder, 'hubconf.py')} (it is not downloaded; run the setup "
              "of the TRELLIS runtime again). / DINOv2 の固定したコードがありません。"
              "TRELLIS 実行環境のセットアップをやり直してください。",
              f"{HUB_MISSING_MARKER}: {os.path.join(folder, 'hubconf.py')} (it is not downloaded; run the setup "
              "of the TRELLIS runtime again).")


def pinned_hub_load(orig_load, get_dir, expected_commit: str, record: dict | None = None, warn=print):
    """Wrap torch.hub.load so that 'facebookresearch/dinov2' (source github, no ref or ':main') is read from
    <get_dir()>/facebookresearch_dinov2_main with source='local'.

    TRELLIS calls torch.hub.load('facebookresearch/dinov2', name, pretrained=True). Without a ref torch.hub asks
    github.com for the default branch on every call before looking at that folder, and fails on GitHub's HTTP
    429 / 5xx / 404 although the pinned code is in place. A missing folder stops with an error instead of
    fetching unpinned code. A PINNED_COMMIT other than expected_commit only warns (the development environment
    keeps an older pin for comparisons; the setup and check_package keep the pin of the distribution).
    Other repositories and sources go to orig_load unchanged.
    """
    def load(repo_or_dir, model, *args, source="github", **kwargs):
        if source == "github" and isinstance(repo_or_dir, str) and repo_or_dir in (DINOV2_REPO, DINOV2_REPO + ":main"):
            folder = dinov2_hub_dir(get_dir())
            if not os.path.isfile(os.path.join(folder, "hubconf.py")):
                raise FileNotFoundError(hub_missing_message(folder))
            commit = read_pinned_commit(folder)
            if record is not None:
                record["dinov2_pinned_commit"] = commit
            if commit != expected_commit:
                warn(f"WARNING: the DINOv2 hub folder is pinned to {commit or 'an unknown commit'}, "
                     f"not {expected_commit}; using it as is", flush=True)
            return orig_load(folder, model, *args, source="local", **kwargs)
        return orig_load(repo_or_dir, model, *args, source=source, **kwargs)
    return load


# --- model files ---------------------------------------------------------------------------------------------
def safetensors_problem(path: str) -> str | None:
    """Cheap check of a .safetensors file: an 8-byte little-endian header length, then a JSON header that fits."""
    size = os.path.getsize(path)
    if size < 8:
        return f"{size} bytes, shorter than the header length field"
    with open(path, "rb") as f:
        n = int.from_bytes(f.read(8), "little")
        first = f.read(1)
    if 8 + n > size:
        return f"header length {n} beyond the file size {size}"
    if first != b"{":
        return "the header is not JSON"
    return None


def check_model_files(model_dir: str) -> list[str]:
    """Problems of the files a TRELLIS pipeline.json names (args.models: <v>.json and <v>.safetensors, relative
    to model_dir) -> ["missing: ckpts/x.safetensors", "damaged: ...", ...]; empty when all look loadable."""
    try:
        with open(os.path.join(model_dir, "pipeline.json"), encoding="utf-8") as f:
            models = json.load(f)["args"]["models"]
        names = [v for v in models.values() if isinstance(v, str)]
    except OSError:
        return ["missing: pipeline.json"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return ["damaged: pipeline.json"]
    out = []
    for v in names:
        for ext in (".json", ".safetensors"):
            rel = v + ext
            p = os.path.join(model_dir, *rel.split("/"))
            if not os.path.isfile(p):
                out.append(f"missing: {rel}")
                continue
            try:
                if ext == ".json":
                    with open(p, encoding="utf-8") as f:
                        json.load(f)
                    problem = None
                else:
                    problem = safetensors_problem(p)
            except (OSError, ValueError) as e:
                problem = type(e).__name__
            if problem:
                out.append(f"damaged: {rel} ({problem})")
    return out


def dinov2_problems(hub_dir: str) -> list[str]:
    """The pinned DINOv2 hub code and weights under <TORCH_HOME>/hub (fetch_models.py puts them there)."""
    out = []
    folder = dinov2_hub_dir(hub_dir)
    if not os.path.isfile(os.path.join(folder, "hubconf.py")):
        out.append(f"missing: {DINOV2_HUB_FOLDER}/hubconf.py ({HUB_MISSING_MARKER})")
    w = os.path.join(hub_dir, "checkpoints", DINOV2_WEIGHTS_NAME)
    if not os.path.isfile(w) or os.path.getsize(w) == 0:
        out.append(f"missing: checkpoints/{DINOV2_WEIGHTS_NAME}")
    return out


def missing_message(where: str, problems: list[str]) -> str:
    lines = "\n".join(f"  {p}" for p in problems)
    return tr(f"{MISSING_MARKER} in {where} (nothing is downloaded; run the setup again to check and re-fetch them):\n"
              f"{lines}\nエラー: 3D 生成のモデルのファイルが無いか壊れています。セットアップをやり直してください"
              "（取得済みのファイルは照合して再利用します）。",
              f"{MISSING_MARKER} in {where} (nothing is downloaded; run the setup again to check and re-fetch them):\n"
              f"{lines}")


def local_only_from_pretrained(orig):
    """Wrap trellis.models.from_pretrained. TRELLIS's Pipeline.from_pretrained retries any failed local load with
    the bare name (``except: models.from_pretrained(v)``), which means a Hugging Face download (offline: an
    error that hides the first one). Only <path>.json + <path>.safetensors that exist are loaded."""
    def from_pretrained(path, **kwargs):
        if os.path.isfile(f"{path}.json") and os.path.isfile(f"{path}.safetensors"):
            return orig(path, **kwargs)
        raise FileNotFoundError(f"{NOT_LOADED_MARKER}: {path} (the local copy is missing or failed to load, see the "
                                "error above; nothing is downloaded. Run the setup again to check and re-fetch it)")
    return from_pretrained


# --- environment ---------------------------------------------------------------------------------------------
def _under(path: str | None, root: str) -> bool:
    if not path:
        return False
    try:
        p, r = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([p, r]) == r
    except ValueError:            # another drive
        return False


def env_warnings(environ, env_root) -> list[str]:
    """Settings that move the pinned DINOv2 / CLIP caches out of --env-root or change the attention backend.
    The app's provider sets all of them; a direct run keeps the caller's values (setdefault), so only warn."""
    root = str(env_root)
    out = []
    hub_cache = (environ.get("HF_HUB_CACHE") or environ.get("HUGGINGFACE_HUB_CACHE")
                 or os.path.join(environ.get("HF_HOME") or "", "hub"))
    for name, value in (("TORCH_HOME", environ.get("TORCH_HOME")), ("HF_HOME", environ.get("HF_HOME")),
                        ("HF_HUB_CACHE", hub_cache)):
        if not _under(value, root):
            out.append(f"WARNING: {name} ({value}) is outside --env-root ({root}); the pinned models there are not used")
    if environ.get("TRANSFORMERS_CACHE"):
        out.append("WARNING: TRANSFORMERS_CACHE is set; transformers may read CLIP from there instead of HF_HOME")
    for name in ("ATTN_BACKEND", "SPARSE_ATTN_BACKEND"):
        v = environ.get(name)
        if v and v != "xformers":
            out.append(f"WARNING: {name}={v}; the runtime environment is set up for xformers")
    return out


# --- input image ---------------------------------------------------------------------------------------------
def normalize_image(image):
    """Apply the EXIF orientation and keep transparency (alpha, LA / PA, a palette or colour-key tRNS) as RGBA,
    everything else RGB: the same as the app's copy_image, so a direct run sees what the app passes."""
    from PIL import ImageOps
    im = ImageOps.exif_transpose(image)
    mode = "RGBA" if (im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info) else "RGB"
    return im.convert(mode)


# --- candidate selection (D-012 / D-013) ---------------------------------------------------------------------
def exclusions(analysis: dict | None, allow_weapons: bool, empty: str | None = None) -> list[str]:
    """Why a candidate cannot be chosen directly (empty: the reason the sample has nothing to analyse)."""
    if empty or analysis is None:
        return [f"empty result ({empty or 'no mesh'})"]
    out = []
    if analysis["arm_angle_deg"] is None:
        out.append("no reliable arm estimate")
    if analysis["weapon_suspected"] and not allow_weapons:
        out.append("weapon-like object: " + ", ".join(analysis["weapon_reasons"]))
    return out


def choose_candidate(cands: list[dict], allow_weapons: bool) -> tuple[dict | None, str | None]:
    """cands: {"seed", "analysis" (None when empty), "excluded"} -> (chosen or None when every candidate is empty,
    warning). Empty candidates are never chosen, not even by the fallbacks."""
    eligible = [c for c in cands if not c["excluded"]]
    if eligible:
        pool, warning = eligible, None
    else:
        usable = [c for c in cands if c["analysis"] is not None]
        if not usable:
            return None, "every candidate is empty"
        # D-013 before D-012: a weapon-free candidate without an arm estimate beats one with a weapon
        weapon_free = [c for c in usable if allow_weapons or not c["analysis"]["weapon_suspected"]]
        if weapon_free:
            pool = weapon_free
            # with --allow-weapons the pool is every usable candidate, so the chosen one may have a weapon
            warning = ("no candidate passed the checks; weapons are allowed, chose a candidate whose arm angle could "
                       "not be estimated" if allow_weapons else
                       "no candidate passed the checks; chose a weapon-free candidate whose arm angle could not be "
                       "estimated")
        else:
            pool = [c for c in usable if c["analysis"]["arm_angle_deg"] is not None] or usable
            warning = "every candidate has a weapon-like object; chose the closest arm angle among them"

    def key(c):
        err = c["analysis"]["arm_angle_error_deg"]
        return (err if err is not None else 1e9, c["seed"])

    return min(pool, key=key), warning


def turned_summary(a: dict) -> dict:
    return {k: a.get(k) for k in ("arm_angle_deg", "pose_class", "weapon_suspected", "weapon_reasons")}


# --- sampler options (SF05) ----------------------------------------------------------------------------------
def sampler_overrides(ss_steps=None, ss_cfg=None, slat_steps=None, slat_cfg=None) -> tuple[dict, dict]:
    """--ss-steps / --ss-cfg / --slat-steps / --slat-cfg -> (sparse structure, structured latent) sampler_params.

    Only the values given are returned; TRELLIS merges them over pipeline.json's params
    ({**pipeline.<stage>_sampler_params, **sampler_params}), so nothing given -> ({}, {}) -> the same call as before."""
    def one(steps, cfg):
        out = {}
        if steps is not None:
            out["steps"] = int(steps)
        if cfg is not None:
            out["cfg_strength"] = float(cfg)
        return out
    return one(ss_steps, ss_cfg), one(slat_steps, slat_cfg)


def multi_image_steps(defaults: dict, overrides: dict) -> int | None:
    """Steps for inject_sampler_multi_image: the overridden steps when given, else pipeline.json's. The stochastic mode
    picks the image of each step from a list of exactly this many entries, so it must match the steps the sampler runs."""
    steps = overrides.get("steps", (defaults or {}).get("steps"))
    return None if steps is None else int(steps)


# --- strict offline mode (SF17) ------------------------------------------------------------------------------
OFFLINE_MARKER = "[offline] blocked network access to"


class OfflineBlocked(ConnectionRefusedError):
    """A connection refused by the strict offline mode (an OSError, so libraries treat it as a network failure)."""


def is_local_host(host) -> bool:
    """Loopback names and addresses (and None / "" = the local machine). Everything else leaves the machine."""
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    h = str(host).strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h in ("", "localhost", "localhost.localdomain") or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def block_network(blocked: list, warn=print):
    """Strict offline mode: refuse every connection and name lookup that leaves the machine, through Python's socket
    module (socket.connect / connect_ex / sendto, getaddrinfo, gethostbyname[_ex]; urllib, requests, huggingface_hub,
    torch.hub and pooch all go through them) and asyncio's Proactor event loop on Windows (IocpProactor.connect /
    sendto, which call ConnectEx / WSASendTo without socket.connect). Loopback (127.0.0.0/8, ::1, localhost) and
    non-IP sockets stay allowed. Proxies are not used either: the *_PROXY variables are removed and NO_PROXY is "*",
    so urllib / requests / httpx neither use them nor read the Windows system proxy, and connect directly (refused
    here) instead of asking a loopback proxy to relay "CONNECT <host>:443" outside.
    Each refused destination ("host:port") is appended to ``blocked`` once and printed with OFFLINE_MARKER; the caller
    gets OfflineBlocked. Native code that opens sockets itself is not covered. Returns a function that undoes it."""
    import socket

    def refuse(host, port=None):
        dest = f"{host}:{port}" if port is not None else str(host)
        if dest not in blocked:
            blocked.append(dest)
            warn(f"{OFFLINE_MARKER} {dest} (strict offline mode)", flush=True)
        raise OfflineBlocked(f"strict offline mode: network access to {dest} is blocked during generation")

    def check_address(sock, address):
        if getattr(sock, "family", None) in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple) \
                and address and not is_local_host(address[0]):
            refuse(address[0], address[1] if len(address) > 1 else None)

    orig = {"connect": socket.socket.connect, "connect_ex": socket.socket.connect_ex, "sendto": socket.socket.sendto,
            "getaddrinfo": socket.getaddrinfo, "gethostbyname": socket.gethostbyname,
            "gethostbyname_ex": socket.gethostbyname_ex}

    def connect(self, address):
        check_address(self, address)
        return orig["connect"](self, address)

    def connect_ex(self, address):
        check_address(self, address)
        return orig["connect_ex"](self, address)

    def sendto(self, data, *args):
        if args:
            check_address(self, args[-1])
        return orig["sendto"](self, data, *args)

    def getaddrinfo(host, port, *args, **kwargs):
        if not is_local_host(host):
            refuse(host, port)
        return orig["getaddrinfo"](host, port, *args, **kwargs)

    def gethostbyname(host):
        if not is_local_host(host):
            refuse(host)
        return orig["gethostbyname"](host)

    def gethostbyname_ex(host):
        if not is_local_host(host):
            refuse(host)
        return orig["gethostbyname_ex"](host)

    socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = connect, connect_ex, sendto
    socket.getaddrinfo, socket.gethostbyname, socket.gethostbyname_ex = getaddrinfo, gethostbyname, gethostbyname_ex

    # asyncio's default loop on Windows connects and sends through the IOCP proactor, not socket.connect / sendto
    # (and skips getaddrinfo for an IP address); these two are its only ways out for TCP and UDP
    try:
        from asyncio import windows_events
        proactor = windows_events.IocpProactor
    except ImportError:          # not Windows
        proactor = None
    if proactor is not None:
        orig["proactor_connect"], orig["proactor_sendto"] = proactor.connect, proactor.sendto

        def proactor_connect(self, conn, address):
            check_address(conn, address)
            return orig["proactor_connect"](self, conn, address)

        def proactor_sendto(self, conn, buf, flags=0, addr=None):
            if addr is not None:
                check_address(conn, addr)
            return orig["proactor_sendto"](self, conn, buf, flags, addr)

        proactor.connect, proactor.sendto = proactor_connect, proactor_sendto

    # a proxy on loopback (HTTPS_PROXY, or the Windows system proxy that urllib reads from the registry when no
    # *_proxy variable is set) would relay the request outside: use no proxy, so the connection is direct and refused
    proxy_env = {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")}
    for k in proxy_env:
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

    def restore():
        socket.socket.connect, socket.socket.connect_ex = orig["connect"], orig["connect_ex"]
        socket.socket.sendto = orig["sendto"]
        socket.getaddrinfo, socket.gethostbyname = orig["getaddrinfo"], orig["gethostbyname"]
        socket.gethostbyname_ex = orig["gethostbyname_ex"]
        if proactor is not None:
            proactor.connect, proactor.sendto = orig["proactor_connect"], orig["proactor_sendto"]
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        os.environ.update(proxy_env)
    return restore


# --- no model downloads during generation (SF17 (2); default, i.e. without --strict-offline) -------------------
DOWNLOAD_MARKER = "[download] blocked"
DOWNLOAD_BLOCKED_TEXT = "model download during generation is blocked"


class DownloadBlocked(RuntimeError):
    """A model file download refused during generation. Not an OSError / ValueError: callers must not take it for a
    network failure (and try something else) or for an image without an object."""


def url_host(url) -> str:
    from urllib.parse import urlsplit
    try:
        return urlsplit(str(url)).hostname or "?"
    except ValueError:
        return "?"


def download_blocked_message(name: str, host: str) -> str:
    """The fixed error (DOWNLOAD_BLOCKED_TEXT: <file> from <host>) with the way out: the setup fetches the file.
    Japanese (the default) keeps the English sentence before the Japanese one; English has the English one only."""
    return tr(f"{DOWNLOAD_BLOCKED_TEXT}: {name} from {host} (the file is not in the TRELLIS runtime and "
              "generation never downloads; run the setup again, or verify the models in the settings, to fetch it). / "
              f"エラー: 生成に使うファイル（{name}）が TRELLIS 実行環境に無いため、生成の途中での取得を止めました"
              "（生成中は取得しません）。左の「セットアップ」を実行するか、設定画面の「TRELLIS 実行環境の管理」の"
              "「モデルを照合し直す…」で取得してください。",
              f"{DOWNLOAD_BLOCKED_TEXT}: {name} from {host} (the file is not in the TRELLIS runtime, and nothing is "
              "downloaded during generation). Run \"Setup\" on the left, or use \"Re-verify models…\" under "
              "\"TRELLIS runtime management\" in Settings, to download it.")


def file_hash_matches(path: str, known_hash) -> bool:
    """pooch's known_hash ("<alg>:<hex>", bare hex = sha256, None = not checked) against the file at path."""
    if known_hash is None:
        return True
    alg, _, want = str(known_hash).rpartition(":")
    try:
        h = hashlib.new((alg or "sha256").lower())
    except ValueError:            # an algorithm hashlib does not have (pooch's xxh*): it cannot be checked here
        return False
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower() == want.strip().lower()


def block_downloads(blocked: list, modules: dict | None = None, warn=print):
    """Default mode (without --strict-offline): a model file that is missing is never downloaded in the middle of a
    generation; the setup fetches and verifies the files (network only during setup).

    * pooch.retrieve (rembg's model download: u2net.onnx when it is not under U2NET_HOME; rembg 2.0.85 uses a file
      that is there as is and calls pooch only when it is missing). The replacement never downloads: an existing
      <path>/<fname> whose hash matches known_hash is returned as pooch would return it, anything else is refused.
    * torch.hub.download_url_to_file (the DINOv2 weights when <TORCH_HOME>/hub/checkpoints has no file; the check
      before loading normally stops first). Always refused.

    Hugging Face stays offline through HF_HUB_OFFLINE=1 and local_only_from_pretrained. A refused download prints
    "<DOWNLOAD_MARKER> <file> from <host> ..." once per file (appended to ``blocked``) and raises DownloadBlocked with
    download_blocked_message. modules: {"pooch": module, "torch.hub": module}; by default they are imported here and a
    module that is not installed is skipped. Returns a function that undoes it."""
    if modules is None:
        modules = {}
        for name in ("pooch", "torch.hub"):
            try:
                modules[name] = importlib.import_module(name)
            except ImportError:
                pass

    def refuse(name, host):
        dest = f"{name} from {host}"
        if dest not in blocked:
            blocked.append(dest)
            warn(f"{DOWNLOAD_MARKER} {dest} (generation does not download; run the setup to fetch it)", flush=True)
        raise DownloadBlocked(download_blocked_message(name, host))

    undo = []
    pooch = modules.get("pooch")
    if pooch is not None and hasattr(pooch, "retrieve"):
        orig_retrieve = pooch.retrieve

        def retrieve(url, known_hash=None, fname=None, path=None, processor=None, downloader=None, progressbar=False):
            name = fname or os.path.basename(str(url).split("?", 1)[0]) or str(url)
            if fname and path is not None:
                full = os.path.join(str(path), fname)
                if os.path.isfile(full) and file_hash_matches(full, known_hash):
                    return processor(full, "fetch", None) if processor is not None else full
            refuse(name, url_host(url))

        pooch.retrieve = retrieve
        undo.append(lambda: setattr(pooch, "retrieve", orig_retrieve))
    hub = modules.get("torch.hub")
    if hub is not None and hasattr(hub, "download_url_to_file"):
        orig_download = hub.download_url_to_file

        def download_url_to_file(url, dst, *args, **kwargs):
            refuse(os.path.basename(str(dst)) or str(dst), url_host(url))

        hub.download_url_to_file = download_url_to_file
        undo.append(lambda: setattr(hub, "download_url_to_file", orig_download))

    def restore():
        for u in reversed(undo):
            u()
    return restore


def orientation_disagreement(front: dict, turned: dict, angle_tol: float = ORIENTATION_ANGLE_TOL) -> list[str]:
    """How the pose / weapon checks of a sample as generated (front assumed +Z) differ from the same sample turned
    180 deg about the vertical axis (front -Z, backlog B-004). Empty = the checks do not depend on the orientation.
    Which one is right is not decided automatically (there is no estimate of the front yet)."""
    out = []
    a, b = front.get("arm_angle_deg"), turned.get("arm_angle_deg")
    if (a is None) != (b is None):
        out.append(f"arm estimate only {'turned 180 deg' if a is None else 'as generated'}")
    elif a is not None and abs(a - b) > angle_tol:
        out.append(f"arm angle {a} deg as generated, {b} deg turned 180 deg")
    if bool(front.get("weapon_suspected")) != bool(turned.get("weapon_suspected")):
        out.append("weapon-like object " + ("only as generated" if front.get("weapon_suspected") else "only turned 180 deg"))
    return out
