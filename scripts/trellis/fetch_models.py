"""Download the model files TRELLIS needs, pinned to fixed revisions.

    python fetch_models.py <env_root> [--text [--accept-clip-notice] [--clip-notice-lang=en]] [--u2net-only] [--dinov2-only]

<env_root> is required (the folder given to setup_trellis_env.ps1 -EnvRoot). --u2net-only and
--dinov2-only fetch only that part into an existing environment and update models/manifest.json.
--clip-notice-lang also takes its value as the next argument (--clip-notice-lang en); an unknown option stops with
the usage before anything is fetched.

Puts
  <env_root>/models/TRELLIS-image-large/            (Hugging Face, pinned revision; only the files pinned in
                                                    models_manifest.json, i.e. not the training-only encoders)
  <env_root>/torch-home/hub/facebookresearch_dinov2_main/   (DINOv2 hub code, pinned commit)
  <env_root>/torch-home/hub/checkpoints/dinov2_vitl14_reg4_pretrain.pth
  <env_root>/u2net/u2net.onnx                       (rembg's u2net for background removal, D-020)
and writes <env_root>/models/manifest.json with sizes and SHA-256 of every file. Downloads are retried on
network errors, and the model files are checked against the pinned sizes and SHA-256 in the
models_manifest*.json next to this script, the DINOv2 hub code against setup_sources.json (a download that only
looks complete is fetched again).
Run TRELLIS with U2NET_HOME=<env_root>/u2net so rembg uses the pinned file instead of downloading.

TRELLIS calls torch.hub.load('facebookresearch/dinov2', 'dinov2_vitl14_reg'), which would
otherwise download the unpinned main branch (and, even with the hub folder in place, ask github.com
for the default branch on every call). generate_glb.py reads the pinned hub folder with
source='local' (TORCH_HOME=<env_root>/torch-home), so generation fetches nothing; without the
folder it stops with an error instead of downloading.
The pin is the last commit before the Cell-DINO / X-Ray DINO code and their non-commercial licence
files were added (D-025). A hub folder from another pin (its PINNED_COMMIT differs) is replaced.

--text additionally fetches text-to-3D (D-005 / D-003). It first shows notices/clip_model_card_notice.ja.txt
(--clip-notice-lang=en: notices/clip_model_card_notice.en.txt) and asks for consent on the console (D-011);
--accept-clip-notice skips the prompt for callers that showed the notice themselves. Consent is stored in
<env_root>/consents.json with the language and the SHA-256 of the notice; a consent stays valid while the notice
of its language is unchanged, whichever language it was given in. It fetches:
  <env_root>/models/TRELLIS-text-xlarge/            (Hugging Face, pinned revision)
  <env_root>/models/TRELLIS-text-xlarge/JeffreyXiang/TRELLIS-image-large/ckpts/*   (hard links)
  <env_root>/hf-home/hub/models--openai--clip-vit-large-patch14/   (HF cache, pinned commit)
and writes <env_root>/models/manifest_text.json.

The text pipeline.json names its decoders "JeffreyXiang/TRELLIS-image-large/ckpts/...". TRELLIS first
looks for <model dir>/<that path>, so hard links to the identical files of TRELLIS-image-large (same
commit 25e0d31, same SHA-256) make it load them locally instead of downloading them again.
CLIPTextModel.from_pretrained('openai/clip-vit-large-patch14') reads the HF cache (HF_HOME=<env_root>/hf-home).
CLIP is fetched at the pinned commit into <env_root>/hf-home/hub, and refs/main is written to point at it (the
same layout as the app's setup); main() sets HF_HOME to <env_root>/hf-home before huggingface_hub is imported,
so a run of this script on its own does not use the user's default Hugging Face cache (audit C39, C40).

Single-file downloads (DINOv2 weights, u2net.onnx) continue a partial <name>.part with an HTTP Range request
when the server supports it, and start again otherwise.
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

TRELLIS_REPO = "microsoft/TRELLIS-image-large"
TRELLIS_REVISION = "25e0d31ffbebe4b5a97464dd851910efc3002d96"
# 2025-06-11 "dino.txt inference code (#528)": newest commit without any Cell-DINO / X-Ray DINO code,
# licence file or non-commercial wording; the ViT code TRELLIS runs is the same as later commits (D-025)
DINOV2_COMMIT = "12592a9bdfea0a6e4ee4767cd5a8d3195c0e3509"
DINOV2_WEIGHTS = "https://dl.fbaipublicfiles.com/dinov2/dinov2_vitl14/dinov2_vitl14_reg4_pretrain.pth"
DINOV2_WEIGHTS_SIZE = 1_217_607_321
DINOV2_WEIGHTS_SHA256 = "36e4deffbaef061a2576705b0c36f93621e2ae20bf6274694821b0b492551b51"
# Background removal for images without transparency (D-020). TRELLIS's preprocess_image calls
# rembg.new_session('u2net'); with U2NET_HOME=<env_root>/u2net rembg finds <env_root>/u2net/u2net.onnx
# (its legacy flat layout) and neither downloads nor re-checks it.
# Licences: rembg MIT; U^2-Net (source of the weights, converted to ONNX by rembg's author) Apache-2.0.
U2NET_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx"
U2NET_SIZE = 175_997_641
U2NET_MD5 = "60024c5c889badc19c04ad937298a77b"  # the hash rembg itself pins for this file
U2NET_SHA256 = "8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491"
TEXT_REPO = "microsoft/TRELLIS-text-xlarge"
TEXT_REVISION = "e0b00432b8e3a8ecee0df806ab1df9f7281f2be4"
DECODER_ALIAS = "JeffreyXiang/TRELLIS-image-large"  # same repo history as microsoft/TRELLIS-image-large
CLIP_REPO = "openai/clip-vit-large-patch14"
CLIP_REVISION = "32bd64288804d66eefd0ccbe215aa642df71cc41"
CLIP_FILES = ["config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json", "vocab.json",
              "merges.txt", "special_tokens_map.json"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


RETRY_DELAYS = (2, 5, 10, 20, 30, 45)  # seconds; the same waits as the app's setup (docs/packaging.md 4.2)
LOCK_WAITS = (1, 2, 4)                 # seconds; a file another program has open for a moment (app: download.LOCK_WAITS)


def _ui_lang() -> str:
    """The app's display language (GDSKIN_LANG, read with notice_lang's rule) for messages not tied to a notice."""
    return notice_lang(os.environ.get("GDSKIN_LANG"))


def retry(what: str, fn):
    """Run fn, retrying network errors (connection resets during the TLS handshake, timeouts, HTTP 5xx).

    urllib, requests and huggingface_hub errors are OSError subclasses, except http.client.IncompleteRead (a response
    cut before its end, with or without Content-Length) and the other http.client.HTTPException errors, which are
    retried as well (the same as fetch_cuda_redist.py and the app's setup, audit R14B-01). HTTP 4xx other than 408/429
    is final. snapshot_download keeps finished files, so a retry continues where it stopped.
    A full disk (ENOSPC) or a read-only one (EROFS) is final at once, and a file that cannot be written
    (PermissionError, e.g. an antivirus scan holding the .part) is tried again only after the short LOCK_WAITS,
    counted apart from the network waits, as in the app's setup (audit R14A-03).
    """
    delays = iter(RETRY_DELAYS)
    locked = 0
    while True:
        try:
            return fn()
        except (OSError, http.client.HTTPException) as e:
            if isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EROFS):
                raise
            if isinstance(e, PermissionError):
                locked += 1
                if locked > len(LOCK_WAITS):
                    raise
                wait = LOCK_WAITS[locked - 1]
                print({"ja": f"{what}: ファイルに書き込めません（{type(e).__name__}: {str(e)[:160]}）。ほかのアプリが開いている"
                             f"可能性があるので、{wait} 秒後にやり直します（{locked}/{len(LOCK_WAITS)}）",
                       "en": f"{what}: cannot write a file ({type(e).__name__}: {str(e)[:160]}). Another app may have it "
                             f"open, so it is retried in {wait} s ({locked}/{len(LOCK_WAITS)})"}[_ui_lang()], flush=True)
                time.sleep(wait)
                continue
            delay = next(delays, None)
            code = getattr(e, "code", None) or getattr(getattr(e, "response", None), "status_code", None)
            if delay is None or (isinstance(code, int) and 400 <= code < 500 and code not in (408, 429)):
                raise
            print(f"{what}: {type(e).__name__}: {str(e)[:160]} - retrying in {delay} s", flush=True)
            time.sleep(delay)


class IncompleteDownload(OSError):
    """Pinned files missing or not matching after a download, or a response shorter than announced; an OSError
    so that retry() runs it again."""


def download(url: str, dest: Path) -> None:
    """Download url into dest (a .part file). A partial dest is continued with a Range request; a server that does
    not answer 206 sends the whole file, which then replaces it. A response that ends before the announced length
    raises IncompleteDownload, so retry() continues from where it stopped (audit C40)."""
    have = dest.stat().st_size if dest.exists() else 0
    req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:  # nothing left to fetch (or a bad range): let the caller's size/SHA-256 check decide
            return
        raise
    with r:
        resume = bool(have) and r.status == 206
        total = None
        rng = r.headers.get("Content-Range") or ""
        if resume and "/" in rng and rng.rsplit("/", 1)[1].strip().isdigit():
            total = int(rng.rsplit("/", 1)[1])
        elif (r.headers.get("Content-Length") or "").strip().isdigit():
            total = int(r.headers["Content-Length"]) + (have if resume else 0)
        with dest.open("ab" if resume else "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    got = dest.stat().st_size
    if total is not None and got < total:
        raise IncompleteDownload(f"{dest.name}: connection closed at {got:,} of {total:,} bytes")


PINNED_DIR = Path(__file__).resolve().parent  # models_manifest.json / models_manifest_text.json ship next to this


def check_pinned(base: Path, files: dict, what: str) -> None:
    """Every pinned file must be in base with the pinned size and SHA-256.

    snapshot_download returns an existing local folder unchecked when the Hub cannot be reached ("Returning
    existing local_dir ... as remote repo cannot be accessed"), e.g. after an interrupted download, so this
    is what proves the download complete. Bad files are deleted so the next attempt fetches them again; in an
    HF cache (snapshots/ linking to blobs/) the blob is deleted too, otherwise the next snapshot_download would
    link the same bad blob again (audit C40).
    """
    bad = [rel for rel, f in files.items() if not (base / rel).is_file()
           or (base / rel).stat().st_size != f["size"] or sha256(base / rel) != f["sha256"]]
    if bad:
        for rel in bad:
            p = base / rel
            real = p.resolve() if p.is_symlink() else None
            p.unlink(missing_ok=True)
            if real is not None:
                real.unlink(missing_ok=True)
        raise IncompleteDownload(f"{what}: {len(bad)} of {len(files)} files missing or not matching the pinned "
                                 f"SHA-256 ({', '.join(bad[:3])}{', ...' if len(bad) > 3 else ''})")


def check_archive(data: bytes, src: dict) -> dict:
    """{path: content} of a source archive ZIP, every file checked against its entry in setup_sources.json.

    The same check as the app's setup (_check_archive): each file with its pinned size and SHA-256, none missing and
    none extra. The ZIP itself is not compared, as GitHub may compress an archive again (docs/packaging.md). A cut or
    broken ZIP (BadZipFile, EOFError, zlib.error) or a mismatch raises IncompleteDownload, so retry() fetches it
    again (audit R14A-01).
    """
    prefix = src["archive_prefix"]
    got = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                rel = info.filename[len(prefix):] if info.filename.startswith(prefix) else None
                want = src["files"].get(rel)
                body = z.read(info)
                if want is None or len(body) != want["size"] or hashlib.sha256(body).hexdigest() != want["sha256"]:
                    raise IncompleteDownload(f"{src['repo']}: {info.filename} in the archive does not match the pinned "
                                             "size/SHA-256")
                got[rel] = body
    except (zipfile.BadZipFile, EOFError, zlib.error) as e:
        raise IncompleteDownload(f"{src['repo']}: broken archive ({type(e).__name__}: {e})") from e
    missing = sorted(set(src["files"]) - set(got))
    if missing:
        raise IncompleteDownload(f"{src['repo']}: {len(missing)} files missing from the archive ({', '.join(missing[:3])})")
    return got


def fetch_u2net(root: Path) -> dict:
    """Place the pinned u2net.onnx at <root>/u2net/u2net.onnx, verified by size, md5 and sha256.

    The download goes to u2net.onnx.part and is renamed only after it matches, so a partial or
    wrong file is never where rembg looks for it.
    """
    info = {"url": U2NET_URL, "size": U2NET_SIZE, "md5": U2NET_MD5, "sha256": U2NET_SHA256}
    dest = root / "u2net" / "u2net.onnx"
    if dest.exists() and dest.stat().st_size == U2NET_SIZE and sha256(dest) == U2NET_SHA256:
        return {**info, "path": str(dest)}
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".onnx.part")
    retry("u2net.onnx", lambda: download(U2NET_URL, tmp))
    md5 = hashlib.md5(tmp.read_bytes()).hexdigest()
    digest = sha256(tmp)
    if tmp.stat().st_size != U2NET_SIZE or md5 != U2NET_MD5 or digest != U2NET_SHA256:
        got = (tmp.stat().st_size, md5, digest)
        tmp.unlink()
        raise RuntimeError(f"u2net.onnx does not match the pinned size/md5/sha256 (got {got})")
    tmp.replace(dest)
    return {**info, "path": str(dest)}


NOTICE_DIR = Path(__file__).resolve().parent / "notices"
# The notice in each display language (decision #2 of 2026-10-18): the app shows the one of its display language
# (English UI -> en, otherwise ja) and records the language and the SHA-256 of the exact text shown.
NOTICE_FILES = {"ja": NOTICE_DIR / "clip_model_card_notice.ja.txt", "en": NOTICE_DIR / "clip_model_card_notice.en.txt"}
NOTICE_FILE = NOTICE_FILES["ja"]  # the default (and the only notice before 2026-10-18)
CONSENT_KEY = "openai/clip-vit-large-patch14"
CONSENT_PROMPTS = {"ja": "上記に同意して取得を続けますか？ [y/N]: ", "en": "Do you agree to the above and continue the download? [y/N]: "}
CONSENT_UNREADABLE = {"ja": "consents.json（同意の記録）を読めなかったため、もう一度確認します。",
                      "en": "consents.json (the consent record) could not be read; asking again."}
# fetch_text stops without consent (in the language of the notice shown)
TEXT_NOT_FETCHED = {"ja": "テキスト→3D 用モデルの取得を中止しました（CLIP の注意事項に同意が得られなかったため）。"
                          "画像→3D はそのまま使えます。 / Text-to-3D models were not fetched: the CLIP notice was not accepted. Image-to-3D can still be used.",
                    "en": "Text-to-3D models were not fetched: the CLIP notice was not accepted. "
                          "Image-to-3D can still be used."}


def notice_lang(lang: str | None) -> str:
    """'en' / 'en-US' -> 'en'; anything else (None, '', 'ja', unknown) -> 'ja' (the same rule as the app's L.NormalizeLanguage)."""
    s = (lang or "").strip().lower()
    return "en" if s == "en" or s.startswith("en-") else "ja"


def notice(lang: str | None = "ja") -> tuple[Path, str, str]:
    """-> (file, text, SHA-256 of the text as shown) of the CLIP notice in that language."""
    path = NOTICE_FILES[notice_lang(lang)]
    text = path.read_text(encoding="utf-8")
    return path, text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_lang(rec: dict) -> str | None:
    """The language of a consent record: notice_lang, else ja (records written before 2026-10-18 have no notice_lang
    and were all for the Japanese notice, so they keep the rule they had: valid while the Japanese notice is
    unchanged). None for a language without a notice, or a value that is not a string (a list or an object stopped
    the check with a TypeError; it is an invalid record, asked again and written anew, audit R14A-05)."""
    lang = rec.get("notice_lang") or "ja"
    return lang if isinstance(lang, str) and lang in NOTICE_FILES else None


def consent_valid(rec: dict | None) -> bool:
    """A consent record is valid when its notice_sha256 is the SHA-256 of the current notice of its recorded language."""
    if not isinstance(rec, dict):
        return False
    lang = record_lang(rec)
    return lang is not None and rec.get("notice_sha256") == notice(lang)[2]


def consent_record(lang: str, method: str) -> dict:
    """The record written to consents.json for the notice of that language (shown as is)."""
    import datetime

    path, _, digest = notice(lang)
    return {
        "model": f"{CLIP_REPO}@{CLIP_REVISION}",
        "notice_file": path.name,
        "notice_lang": notice_lang(lang),
        "notice_sha256": digest,
        "accepted_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": method,
    }


def load_consents(path: Path) -> tuple[dict, bool]:
    """-> (records in consents.json, whether the file is there but unreadable).

    A missing file has no records. An empty, cut or non-object file (e.g. a write stopped by a power cut) is read as
    no records too, so consent is asked again and the file written anew instead of stopping with a traceback
    (audit R14B-03).
    """
    if not path.exists():
        return {}, False
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, True
    return (records, False) if isinstance(records, dict) else ({}, True)


def save_consents(path: Path, records: dict) -> None:
    """Write consents.json through consents.json.tmp and os.replace (the same as the app's consent.accept), so a write
    that stops half way never leaves a cut consents.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def ensure_clip_consent(root: Path, accept_flag: bool, lang: str = "ja") -> bool:
    """Show the CLIP model-card notice and record consent in <root>/consents.json (D-011).

    Consent is asked once per notice text: a record that is valid for the current notice of its own language
    (consent_valid) is reused, whichever language it was given in. Otherwise the notice in ``lang`` (ja by default,
    --clip-notice-lang=en for English) is shown and the record keeps that language and the SHA-256 of that text.
    accept_flag (--accept-clip-notice) is for non-interactive callers that showed the same notice themselves
    (e.g. the app's setup screen).
    """
    path = root / "consents.json"
    records, broken = load_consents(path)
    if broken:
        print(CONSENT_UNREADABLE[notice_lang(lang)], flush=True)
    previous = records.get(CONSENT_KEY)
    if consent_valid(previous):
        print(f"CLIP notice already accepted on {previous.get('accepted_at')} ({previous.get('method')}, "
              f"{record_lang(previous)})")
        return True
    lang = notice_lang(lang)
    print(notice(lang)[1], flush=True)
    if accept_flag:
        method = "flag --accept-clip-notice"
    else:
        try:
            answer = input(CONSENT_PROMPTS[lang])
        except EOFError:  # no console (non-interactive run)
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            return False
        method = "console"
    records[CONSENT_KEY] = consent_record(lang, method)
    save_consents(path, records)
    return True


def fetch_text(root: Path, accept_flag: bool = False, lang: str = "ja") -> None:
    from huggingface_hub import snapshot_download

    if not ensure_clip_consent(root, accept_flag, lang):
        raise SystemExit(TEXT_NOT_FETCHED[notice_lang(lang)])

    models = root / "models"
    text_dir = models / "TRELLIS-text-xlarge"
    pinned = json.loads((PINNED_DIR / "models_manifest_text.json").read_text(encoding="utf-8"))
    own = {k: v for k, v in pinned["trellis_text"]["files"].items() if k not in pinned["trellis_text"]["linked_decoders"]}

    def get_text() -> None:
        snapshot_download(TEXT_REPO, revision=TEXT_REVISION, local_dir=text_dir)
        check_pinned(text_dir, own, TEXT_REPO)
    retry(TEXT_REPO, get_text)

    config = json.loads((text_dir / "pipeline.json").read_text(encoding="utf-8"))["args"]["models"]
    linked = []
    for ref in config.values():
        if not ref.startswith(DECODER_ALIAS + "/"):
            continue
        for ext in (".json", ".safetensors"):
            src = models / "TRELLIS-image-large" / (ref[len(DECODER_ALIAS) + 1:] + ext)
            dst = text_dir / (ref + ext)
            rel = dst.relative_to(text_dir).as_posix()
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and dst.stat().st_size != pinned["trellis_text"]["files"][rel]["size"]:
                dst.unlink()  # a copy cut short by an earlier run (audit R14A-02)
            if not dst.exists():
                try:
                    os.link(src, dst)
                except OSError:
                    # no hard links (e.g. exFAT): copy under another name first, so a stopped copy is never dst
                    tmp = dst.with_name(dst.name + ".part")
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dst)
            linked.append(rel)
    # the links / copies have the same pinned sizes and SHA-256 as the rest; a bad one is deleted, so a run again
    # makes it anew
    check_pinned(text_dir, {rel: pinned["trellis_text"]["files"][rel] for rel in linked}, "decoders linked from "
                 "TRELLIS-image-large")

    # CLIP at the pinned commit (not "main"), always in <root>/hf-home/hub even if the user set HF_HUB_CACHE, i.e. the
    # place the app's setup uses (setup_manifest.json: cache hf-home/hub) and generation reads (HF_HOME=<root>/hf-home)
    cache = root / "hf-home" / "hub"

    def get_clip() -> Path:
        d = Path(snapshot_download(CLIP_REPO, revision=CLIP_REVISION, allow_patterns=CLIP_FILES, cache_dir=cache))
        check_pinned(d, pinned["clip"]["files"], CLIP_REPO)
        return d
    clip_dir = retry(CLIP_REPO, get_clip)
    if clip_dir.name != CLIP_REVISION:
        raise RuntimeError(f"{CLIP_REPO}: snapshot {clip_dir.name}, expected {CLIP_REVISION}")
    # snapshot_download does not write refs/main for a commit hash; transformers resolves "main" through it offline
    refs = clip_dir.parent.parent / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(CLIP_REVISION, encoding="ascii")

    manifest = {"trellis_text": {"repo": TEXT_REPO, "revision": TEXT_REVISION, "files": {}, "linked_decoders": linked},
                "clip": {"repo": CLIP_REPO, "revision": CLIP_REVISION, "files": {}}}
    for base, key in ((text_dir, "trellis_text"), (clip_dir, "clip")):
        for p in sorted(base.rglob("*")):
            if p.is_file() and ".cache" not in p.parts:
                manifest[key]["files"][p.relative_to(base).as_posix()] = {"size": p.stat().st_size, "sha256": sha256(p)}
    own = {k: v for k, v in manifest["trellis_text"]["files"].items() if k not in linked}
    manifest["trellis_text"]["total_bytes_downloaded"] = sum(v["size"] for v in own.values())
    manifest["clip"]["total_bytes"] = sum(v["size"] for v in manifest["clip"]["files"].values())
    (models / "manifest_text.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"TRELLIS-text-xlarge: {manifest['trellis_text']['total_bytes_downloaded']:,} bytes downloaded, "
          f"{len(linked)} decoder files linked from TRELLIS-image-large")
    print(f"CLIP {CLIP_REPO}@{CLIP_REVISION[:8]}: {manifest['clip']['total_bytes']:,} bytes in {clip_dir}")


def fetch_dinov2(hub: Path) -> dict:
    """Pinned DINOv2 hub code at <hub>/facebookresearch_dinov2_main and the ViT-L/14 reg4 weights.

    The code folder is rebuilt when its PINNED_COMMIT is missing or names another commit (an environment
    made with an earlier pin); it is extracted next to it first and swapped in only when complete. Every file
    of the archive is checked against setup_sources.json (check_archive) before it is extracted. The
    weights are verified by size and SHA-256 and fetched again when they do not match.
    """
    dinov2_dir = hub / "facebookresearch_dinov2_main"
    marker = dinov2_dir / "PINNED_COMMIT"
    current = marker.read_text(encoding="ascii").strip() if marker.exists() else None
    if current != DINOV2_COMMIT or not (dinov2_dir / "hubconf.py").exists():
        url = f"https://github.com/facebookresearch/dinov2/archive/{DINOV2_COMMIT}.zip"
        src = json.loads((PINNED_DIR / "setup_sources.json").read_text(encoding="utf-8"))["sources"]["dinov2"]
        if src["commit"] != DINOV2_COMMIT:
            raise RuntimeError(f"setup_sources.json pins DINOv2 {src['commit'][:7]}, this script {DINOV2_COMMIT[:7]}")
        staging = hub / "facebookresearch_dinov2_main.part"

        def get_archive() -> None:
            # fetch, check and extract in one retried step, so a cut or broken archive is fetched again (audit R14A-01)
            if staging.exists():
                shutil.rmtree(staging)
            with urllib.request.urlopen(url, timeout=60) as r:
                files = check_archive(r.read(), src)
            for rel, body in files.items():
                out = staging / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(body)
        retry("DINOv2 hub code", get_archive)
        (staging / "PINNED_COMMIT").write_text(DINOV2_COMMIT + "\n", encoding="ascii")
        if dinov2_dir.exists():
            shutil.rmtree(dinov2_dir)
        staging.rename(dinov2_dir)
        if current and current != DINOV2_COMMIT:
            print(f"DINOv2 hub code: replaced {current[:7]} with {DINOV2_COMMIT[:7]}")

    weights = hub / "checkpoints" / Path(DINOV2_WEIGHTS).name
    weights.parent.mkdir(parents=True, exist_ok=True)
    if not (weights.exists() and weights.stat().st_size == DINOV2_WEIGHTS_SIZE
            and sha256(weights) == DINOV2_WEIGHTS_SHA256):
        tmp = weights.with_suffix(".part")
        retry("DINOv2 weights", lambda: download(DINOV2_WEIGHTS, tmp))
        got = (tmp.stat().st_size, sha256(tmp))
        if got != (DINOV2_WEIGHTS_SIZE, DINOV2_WEIGHTS_SHA256):
            tmp.unlink()
            raise RuntimeError(f"{weights.name} does not match the pinned size/sha256 (got {got})")
        tmp.replace(weights)
    return {"commit": DINOV2_COMMIT, "weights_url": DINOV2_WEIGHTS,
            "weights": {"size": DINOV2_WEIGHTS_SIZE, "sha256": DINOV2_WEIGHTS_SHA256}}


def update_manifest(models: Path, key: str, info: dict) -> None:
    path = models / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    manifest[key] = info
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# main() without <env_root>, in the app's display language (GDSKIN_LANG, read with notice_lang's rule)
USAGE = {"ja": "usage: python fetch_models.py <env_root> [--text [--accept-clip-notice] [--clip-notice-lang=en]] "
               "[--u2net-only] [--dinov2-only]\n"
               "<env_root> (the TRELLIS environment folder) is required.\n"
               "<env_root>（TRELLIS 実行環境のフォルダー）を指定してください。",
         "en": "usage: python fetch_models.py <env_root> [--text [--accept-clip-notice] [--clip-notice-lang=en]] "
               "[--u2net-only] [--dinov2-only]\n"
               "<env_root> (the TRELLIS environment folder) is required."}
OPTIONS = ("--text", "--accept-clip-notice", "--u2net-only", "--dinov2-only")


def parse_args(argv: list[str]) -> tuple[Path, set[str], str]:
    """-> (<env_root>, the options given, the --clip-notice-lang value) of the command line.

    --clip-notice-lang takes its value after = or as the next argument (--clip-notice-lang en). An unknown option, a
    --clip-notice-lang without a value, or more than one <env_root> stops with the usage (exit code 1, in the display
    language) before anything is made: a misspelt option no longer starts the whole download, and the 'en' of
    '--clip-notice-lang en' is never taken for <env_root> (audit R14A-04).
    """
    usage = USAGE["en"].splitlines()[0]
    roots, opts, lang = [], set(), "ja"
    rest = list(argv)
    while rest:
        a = rest.pop(0)
        if a == "--clip-notice-lang":
            if not rest or rest[0].startswith("-"):
                sys.exit(usage + "\n" + {"ja": "--clip-notice-lang の後に言語（ja か en）を指定してください。",
                                         "en": "--clip-notice-lang needs a language (ja or en)."}[_ui_lang()])
            lang = rest.pop(0)
        elif a.startswith("--clip-notice-lang="):
            lang = a.split("=", 1)[1]
        elif a in OPTIONS:
            opts.add(a)
        elif a.startswith("-"):
            sys.exit(usage + "\n" + {"ja": f"知らないオプションです: {a}", "en": f"unknown option: {a}"}[_ui_lang()])
        else:
            roots.append(a)
    if not roots:
        sys.exit(USAGE[_ui_lang()])
    if len(roots) > 1:
        sys.exit(usage + "\n" + {"ja": f"<env_root> は 1 つだけ指定してください: {' '.join(roots)}",
                                 "en": f"give only one <env_root>: {' '.join(roots)}"}[_ui_lang()])
    return Path(roots[0]), opts, lang


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # the CLIP notice is Japanese; keep UTF-8 when piped
        stream.reconfigure(encoding="utf-8")
    root, opts, lang = parse_args(sys.argv[1:])
    models = root / "models"
    hub = root / "torch-home" / "hub"
    models.mkdir(parents=True, exist_ok=True)
    (hub / "checkpoints").mkdir(parents=True, exist_ok=True)

    if "--u2net-only" in opts:  # add background removal to an existing environment
        info = fetch_u2net(root)
        update_manifest(models, "u2net", info)
        print(f"u2net: {info['size']:,} bytes in {info['path']} (sha256 verified)")
        return 0
    if "--dinov2-only" in opts:  # re-pin the DINOv2 hub code of an existing environment (D-025)
        info = fetch_dinov2(hub)
        update_manifest(models, "dinov2", info)
        print(f"DINOv2 hub code {DINOV2_COMMIT[:7]} and weights (sha256 verified) in {hub}")
        return 0

    # huggingface_hub reads these when it is imported, so set them first (setup_trellis_env.ps1 does the same; a
    # setdefault after the import had no effect and CLIP went to the user's default cache, audit C39)
    os.environ["HF_HOME"] = str((root / "hf-home").resolve())
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    # this script is for fetching: the user's offline mode made every model fail after 112 s of retries (audit R14A-03),
    # and hf_transfer is not in the pinned packages, so with HF_HUB_ENABLE_HF_TRANSFER=1 every download stopped with a
    # ValueError (audit R14B-02). Only this process is changed
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
    from huggingface_hub import snapshot_download

    trellis_dir = models / "TRELLIS-image-large"
    pinned = json.loads((PINNED_DIR / "models_manifest.json").read_text(encoding="utf-8"))["trellis"]["files"]

    def get_trellis() -> None:
        # only the pinned files: the training-only encoders (ckpts/ss_enc_*, ckpts/slat_enc_*, about 292 MB) are not
        # named by either pipeline.json, so they are not fetched (the same set as the app's setup, audit SF18)
        snapshot_download(TRELLIS_REPO, revision=TRELLIS_REVISION, local_dir=trellis_dir, allow_patterns=sorted(pinned))
        check_pinned(trellis_dir, pinned, TRELLIS_REPO)
    retry(TRELLIS_REPO, get_trellis)

    dinov2 = fetch_dinov2(hub)
    weights = hub / "checkpoints" / Path(DINOV2_WEIGHTS).name
    u2net = fetch_u2net(root)

    manifest = {
        "trellis": {"repo": TRELLIS_REPO, "revision": TRELLIS_REVISION, "files": {}},
        "dinov2": dinov2,
        "u2net": u2net,
    }
    total = 0
    for p in sorted(trellis_dir.rglob("*")):
        if p.is_file() and ".cache" not in p.parts:
            size = p.stat().st_size
            total += size
            manifest["trellis"]["files"][p.relative_to(trellis_dir).as_posix()] = {"size": size, "sha256": sha256(p)}
    manifest["trellis"]["total_bytes"] = total
    (models / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"TRELLIS-image-large: {total:,} bytes in {trellis_dir}")
    print(f"DINOv2 hub code {DINOV2_COMMIT[:7]}, weights {weights.stat().st_size:,} bytes in {weights} (sha256 verified)")
    print(f"u2net: {u2net['size']:,} bytes in {u2net['path']} (sha256 verified)")
    if "--text" in opts:
        fetch_text(root, accept_flag="--accept-clip-notice" in opts, lang=lang)
    return 0


if __name__ == "__main__":
    sys.exit(main())
