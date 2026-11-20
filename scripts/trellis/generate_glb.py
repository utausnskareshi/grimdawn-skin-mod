"""Image or text -> 3D with TRELLIS, exporting GLB files and measurements.

Run with the interpreter of a TRELLIS environment (runtime or dev profile), e.g.

    <env_root>\\venv\\Scripts\\python.exe -I generate_glb.py ^
        --image <rgba.png> --out <dir> --variants 0.9:1024 0.95:1024

<env_root> (models, sources, torch-home) defaults to the parent folder of that venv (--env-root).

Inputs (one of):
  --image <png>        microsoft/TRELLIS-image-large (models/TRELLIS-image-large). Give it 1 to 4 times:
                       one image uses pipeline.run's steps, several use run_multi_image's (views of the same
                       object such as front/back/side; TRELLIS uses no camera poses). --multi-mode picks how
                       the images condition the samplers, --image-label names each image in the report.
  --prompt "<text>"    microsoft/TRELLIS-text-xlarge (models/TRELLIS-text-xlarge, fetch_models.py --text)
  --batch <json>       several text jobs with one model load: [{"name": ..., "prompt": ..., "seed": ...}, ...]
  --from-dump <pt>     no generation; post-process a sample saved with --dump / --dump-dir

Post-processing (GLB conversion), D-010:
  --postprocess alt      default. postprocess_alt.to_glb_alt: permissive licences only, no compiled
                         extensions (no nvdiffrast / diff-gaussian-rasterization / pymeshfix / igraph).
  --postprocess trellis  TRELLIS's original postprocessing_utils.to_glb. Development comparison only;
                         needs the dev profile (non-commercial and GPL libraries) and stops with an
                         error before loading any model when they are missing.

Each variant is "simplify:texture_size". All variants reuse the same generated sample, so they differ
only in post-processing. <name>_metrics.json records per-stage time, VRAM peaks (PyTorch allocator and
NVML device-wide), and the vertex/triangle counts, texture size and bounds read back from each GLB.
For image jobs it also records input_images (count, and per image: label, whether the background was
removed, SHA-256 of the file, the preprocessed output file; no input file names or image data) and
multi_mode (null for one image). Preprocessed images: <name>_preprocessed.png for image 1,
<name>_preprocessed_<k>.png for image k >= 2.

Nothing is downloaded: the pinned DINOv2 hub code is read from <TORCH_HOME>/hub/facebookresearch_dinov2_main with
source='local' (D-025; torch.hub would otherwise ask github.com on every load), and the model files named by
pipeline.json are checked before loading ("ERROR: model files are missing or damaged ..." when one is not usable).
A file that is still missing when a library wants it (rembg's u2net.onnx for an opaque image, the DINOv2 weights) is
not downloaded either: pooch.retrieve and torch.hub.download_url_to_file are replaced (gen_helpers.block_downloads)
and the run stops with "model download during generation is blocked: <file> from <host>" (with --strict-offline the
socket block refuses it instead).
Text prompts are fitted into CLIP's 77 tokens so that D-013's "no weapon" is kept (prompt_fit.py); the report
records prompt_tokens / prompt_truncated / no_weapon_kept. A sample with nothing generated is never chosen and stops
the job with "ERROR: no object was generated" when no candidate is left.

Optional (all off by default; without them the run is the same as before):
  --ss-steps/--ss-cfg/--slat-steps/--slat-cfg  sampler steps and guidance strength of the two stages (SF05; default:
                       pipeline.json). Several images pass the same steps to inject_sampler_multi_image. The report
                       records sampler_overrides and the resulting sampler_params.
  --low-vram           move the conditioning model (DINOv2 / CLIP) and the sampling models to the CPU while decoding
                       (SF15): lower VRAM peak, same weights and computation; costs the transfer time per candidate.
  --strict-offline     refuse every network connection that leaves the machine (Python sockets) during the whole run
                       and print "[offline] blocked network access to <host:port>" (SF17); the report lists them.
                       No proxy is used (HTTP(S)_PROXY or the Windows system proxy), so a proxy on loopback cannot
                       relay a request outside; the request goes direct and is refused.
  --sample-dir <dir>   save every generated sample that is not empty (each candidate of --select-pose, or the one
                       sample) as <dir>/<name>_seed<seed>.pt, about 50 MB each (SF14), so that a candidate can be turned
                       into a GLB later with --from-dump (read with weights_only=True) without generating again. Unlike
                       --dump-dir the chosen candidate is not written twice. The report lists them (saved_samples) and
                       <name>_selection.json names each candidate's file (sample). A sample that cannot be saved
                       (disk full, ...) is skipped with "[sample] WARNING: seed N not saved" and listed under
                       sample_save_failed; the generation goes on.
  --variant-base <glb> text-guided appearance variant of an existing result (SF10, TrellisTextTo3DPipeline.run_variant):
                       the GLB (y-up, front +Z, as the app's generated.glb) is turned back into TRELLIS's z-up space
                       (variant_base.py) and voxelized with pipeline.voxelize (64^3); the sparse structure is not sampled,
                       only the structured latent from the text (get_cond, manual_seed, sample_slat, decode: the steps of
                       run_variant), so the shape and pose are kept and the appearance changes. Text input only (--prompt
                       or --batch); not with --select-pose / --ss-steps / --ss-cfg. The report records variant (SHA-256
                       of the base file, its counts and z-up extent, voxels; no file name); its sampler_params (with
                       --slat-steps / --slat-cfg) has the slat stage only.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

# Packages only the original TRELLIS to_glb needs (removed from the runtime profile by D-010).
TRELLIS_TO_GLB_MODULES = {
    "nvdiffrast": "NVIDIA Source Code License, non-commercial",
    "diff_gaussian_rasterization": "Gaussian-Splatting License, non-commercial",
    "pymeshfix": "GPL-3.0",
    "igraph": "GPL-2.0-or-later",
}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", type=Path, action="append",
                     help="input image (TRELLIS-image-large), 1 to 4 times: one -> pipeline.run, several -> "
                          "run_multi_image (views of the same object). The EXIF orientation is applied first; an "
                          "image with transparency (alpha or a tRNS colour key) is used as is, otherwise the "
                          "background is removed with rembg u2net (D-020)")
    src.add_argument("--prompt", help="text prompt (TRELLIS-text-xlarge)")
    src.add_argument("--batch", type=Path, help='JSON list of {"name", "prompt", "seed"} text jobs')
    src.add_argument("--from-dump", type=Path, help="skip generation, post-process this saved sample")
    ap.add_argument("--image-label", action="append", default=None,
                    help="optional name per --image, same count and order (e.g. front, back, left, right); "
                         "recorded in the report only, not used for generation")
    ap.add_argument("--multi-mode", choices=["stochastic", "multidiffusion"], default="stochastic",
                    help="several images: stochastic conditions each sampler step on one image in turn (same time "
                         "as one image); multidiffusion averages the predictions of all images every step "
                         "(time grows with the count). Ignored for one image")
    ap.add_argument("--max-images", type=int, default=4, help=argparse.SUPPRESS)  # evaluation only
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--name", default=None, help="output file stem (default: image or dump stem; required with --prompt)")
    ap.add_argument("--env-root", type=Path, default=Path(sys.prefix).parent,
                    help="environment root with models/, src/, torch-home/ (default: parent of the venv)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--variants", nargs="*", default=["0.95:1024"],
                    help="simplify:texture_size; give none to only generate (and dump)")
    ap.add_argument("--postprocess", choices=["alt", "trellis"], default="alt",
                    help="alt (default): permissive-licence GLB conversion; trellis: original to_glb, "
                         "development comparison only")
    ap.add_argument("--texture-source", choices=["gaussian", "vertex", "gaussian-density"], default="gaussian",
                    help="alt only: colour source for the texture")
    ap.add_argument("--fill-holes-views", type=int, default=1000,
                    help="alt only: views for hidden-face removal (fewer views can skip whole cleanups)")
    ap.add_argument("--fill-holes-resolution", type=int, default=1024, help="alt only: raster size for it")
    ap.add_argument("--runs", type=int, default=1, help="generate this many times (timing: run 1 is cold)")
    ap.add_argument("--select-pose", action="store_true",
                    help="D-012: generate --candidates seeds and keep the one closest to the bind-pose arm angle")
    ap.add_argument("--candidates", type=int, default=4, help="candidates per round for --select-pose")
    ap.add_argument("--max-rounds", type=int, default=2,
                    help="--select-pose: rounds of new seeds while every candidate is excluded")
    ap.add_argument("--target-arm-angle", type=float, default=40.0,
                    help="arm angle from straight down of the game's bind pose (degrees, docs/converter.md 2.2)")
    ap.add_argument("--allow-weapons", action="store_true",
                    help="do not append 'no weapon' to text prompts and do not exclude weapon-like candidates")
    ap.add_argument("--spconv-algo", default="native", choices=["native", "auto"])
    # SF05: not given = pipeline.json (image: 25 steps, cfg 5.0 for both stages; text: 25 steps, cfg 7.5)
    ap.add_argument("--ss-steps", type=int, default=None, help="sparse structure sampler steps (default: pipeline.json)")
    ap.add_argument("--ss-cfg", type=float, default=None,
                    help="sparse structure guidance strength (default: pipeline.json)")
    ap.add_argument("--slat-steps", type=int, default=None,
                    help="structured latent sampler steps (default: pipeline.json)")
    ap.add_argument("--slat-cfg", type=float, default=None,
                    help="structured latent guidance strength (default: pipeline.json)")
    ap.add_argument("--low-vram", action="store_true",
                    help="SF15: move the conditioning and sampling models to the CPU while decoding (lower VRAM peak)")
    ap.add_argument("--strict-offline", action="store_true",
                    help="SF17: refuse every network connection that leaves the machine and log its destination")
    ap.add_argument("--no-views", action="store_true", help="skip the orthographic check renders")
    ap.add_argument("--dump", type=Path, default=None, help="save the raw sample here (single job)")
    ap.add_argument("--dump-dir", type=Path, default=None, help="save each job's raw sample as <dir>/<name>.pt")
    ap.add_argument("--sample-dir", type=Path, default=None,
                    help="SF14: save every non-empty sample (each candidate, or the one sample) as "
                         "<dir>/<name>_seed<seed>.pt for --from-dump (the chosen candidate is not written twice)")
    ap.add_argument("--variant-base", type=Path, default=None,
                    help="SF10: text-guided appearance variant (run_variant) of this GLB (y-up, as the app's "
                         "generated.glb): its voxels are kept, only the appearance is sampled from the text")
    args = ap.parse_args()
    if args.variant_base is not None:
        if args.prompt is None and args.batch is None:
            ap.error("--variant-base needs a text (--prompt or --batch)")
        if args.select_pose:
            ap.error("--variant-base keeps the base's shape and pose; it cannot be used with --select-pose")
        if args.ss_steps is not None or args.ss_cfg is not None:
            ap.error("--variant-base does not sample the sparse structure; --ss-steps / --ss-cfg are not used with it")
    if args.sample_dir is not None and args.from_dump is not None:
        ap.error("--sample-dir saves generated samples; it cannot be used with --from-dump")
    if args.prompt is not None and not args.name:
        ap.error("--name is required with --prompt")
    if args.image_label is not None:
        if args.image is None:
            ap.error("--image-label needs --image")
        if len(args.image_label) != len(args.image):
            ap.error(f"--image-label was given {len(args.image_label)} times but --image {len(args.image)} times")
        if any(not s.strip() or len(s) > 64 for s in args.image_label):
            ap.error("--image-label must be 1 to 64 characters")
    if args.image is not None and len(args.image) > args.max_images:
        ap.error(f"--image can be given at most {args.max_images} times")
    for name in ("ss_steps", "slat_steps"):
        v = getattr(args, name)
        if v is not None and not 1 <= v <= 100:
            ap.error(f"--{name.replace('_', '-')} must be 1 to 100")
    for name in ("ss_cfg", "slat_cfg"):
        v = getattr(args, name)
        if v is not None and not 0.0 <= v <= 20.0:
            ap.error(f"--{name.replace('_', '-')} must be 0 to 20")
    return args


ARGS = parse_args()
for _stream in (sys.stdout, sys.stderr):  # UTF-8 even when piped (Windows defaults to the ANSI code page)
    _stream.reconfigure(encoding="utf-8")

if ARGS.postprocess == "trellis" and ARGS.variants:
    missing = [m for m in TRELLIS_TO_GLB_MODULES if importlib.util.find_spec(m) is None]
    if missing:
        # gen_helpers.tr for the display language; this runs before the script folder is put on sys.path below (-I)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gen_helpers  # noqa: E402
        lines = "\n".join(f"  - {m} ({TRELLIS_TO_GLB_MODULES[m]})" for m in missing)
        sys.exit(gen_helpers.tr(
            "ERROR: --postprocess trellis uses TRELLIS's original to_glb, a development-only comparison mode.\n"
            f"This environment ({sys.prefix}) lacks:\n{lines}\n"
            "Use a development environment (setup_trellis_env.ps1 -Mode dev -EnvRoot <folder>),\n"
            "or omit --postprocess to use the default permissive-licence conversion.\n"
            "エラー: --postprocess trellis は開発用の比較手段です。この環境には上記のライブラリ（非商用・GPL）が"
            "入っていません。開発用の環境で実行するか、--postprocess を外して既定の変換を使ってください。",
            "ERROR: --postprocess trellis uses TRELLIS's original to_glb, a development-only comparison mode.\n"
            f"This environment ({sys.prefix}) lacks:\n{lines}\n"
            "Use a development environment (setup_trellis_env.ps1 -Mode dev -EnvRoot <folder>),\n"
            "or omit --postprocess to use the default permissive-licence conversion."))

ENV = ARGS.env_root
os.environ.setdefault("ATTN_BACKEND", "xformers")  # flash-attn has no sm_120 Windows wheel
os.environ.setdefault("SPCONV_ALGO", ARGS.spconv_algo)
os.environ.setdefault("TORCH_HOME", str(ENV / "torch-home"))  # pinned DINOv2 hub code + weights
os.environ.setdefault("HF_HOME", str(ENV / "hf-home"))  # pinned CLIP text encoder (text mode)
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("WARP_CACHE_PATH", str(ENV / "warp-cache"))  # kaolin imports NVIDIA warp
os.environ.setdefault("U2NET_HOME", str(ENV / "u2net"))  # rembg's u2net.onnx for opaque images (D-020)
sys.path.insert(0, str(ENV / "src" / "TRELLIS"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_helpers  # noqa: E402

# SF17: before numpy / torch / TRELLIS are imported, so that nothing they do can reach the network
OFFLINE_BLOCKED: list[str] = []
if ARGS.strict_offline:
    gen_helpers.block_network(OFFLINE_BLOCKED)
SS_PARAMS, SLAT_PARAMS = gen_helpers.sampler_overrides(ARGS.ss_steps, ARGS.ss_cfg, ARGS.slat_steps, ARGS.slat_cfg)

import prompt_fit  # noqa: E402
from fetch_models import DINOV2_COMMIT  # noqa: E402

# The app sets these to the runtime environment; a direct run keeps the caller's values (setdefault above,
# e.g. another TORCH_HOME for a comparison), so only say when they point elsewhere.
for _w in gen_helpers.env_warnings(os.environ, ENV):
    print(_w, flush=True)

import numpy as np  # noqa: E402
import torch  # noqa: E402
from PIL import Image  # noqa: E402

# D-025: the pinned DINOv2 hub code (12592a9) predates its own weights_only=True loading (#598), and
# torch.hub.load_state_dict_from_url defaults to weights_only=False; load checkpoints as plain tensors.
_hub_load_state_dict = torch.hub.load_state_dict_from_url


def _load_state_dict_weights_only(*args, **kwargs):
    kwargs.setdefault("weights_only", True)
    return _hub_load_state_dict(*args, **kwargs)


torch.hub.load_state_dict_from_url = _load_state_dict_weights_only
# D-025: torch.hub.load('facebookresearch/dinov2', ...) without a ref asks github.com for the default branch on
# every call before it looks at the pinned folder (and fails on GitHub's 429 / 5xx / 404). Read the folder with
# source='local' instead; a missing folder stops with an error, unpinned code is never fetched.
HUB_INFO: dict = {}
torch.hub.load = gen_helpers.pinned_hub_load(torch.hub.load, torch.hub.get_dir, DINOV2_COMMIT, HUB_INFO)
# SF17 (2), default: a missing model file is never downloaded in the middle of a generation (rembg's pooch would fetch
# u2net.onnx from github.com, torch.hub the DINOv2 weights); the run stops with DownloadBlocked and the setup fetches it.
# --strict-offline refuses the connections at the socket level instead (above, unchanged).
DOWNLOAD_BLOCKED: list[str] = []
if not ARGS.strict_offline:
    gen_helpers.block_downloads(DOWNLOAD_BLOCKED)

from measure import Stage, glb_stats, nvml_used_mib, process_peak, render_views  # noqa: E402
import pose_check  # noqa: E402

GAUSSIAN_KEYS = ("_xyz", "_features_dc", "_scaling", "_rotation", "_opacity")
# SF10 (--variant-base): {"coords": the base's voxels with the batch column, as run_variant builds them}
VARIANT: dict = {}


def sample_to_raw(mesh, gauss, meta: dict) -> dict:
    """Raw sample (CPU tensors): FlexiCubes mesh with decoder vertex colours, and Gaussian parameters.

    This is both the dump file format and the input of postprocess_alt.to_glb_alt.
    """
    return {
        "mesh": {
            "vertices": mesh.vertices.detach().cpu(),
            "faces": mesh.faces.detach().cpu(),
            "vertex_attrs": None if mesh.vertex_attrs is None else mesh.vertex_attrs.detach().cpu(),
            "res": mesh.res,
        },
        "gaussian": {"init_params": gauss.init_params, **{k: getattr(gauss, k).detach().cpu() for k in GAUSSIAN_KEYS}},
        "meta": meta,
    }


def raw_to_sample(raw: dict):
    """Rebuild TRELLIS MeshExtractResult / Gaussian objects (for the original to_glb)."""
    from trellis.representations import Gaussian, MeshExtractResult
    m = raw["mesh"]
    mesh = MeshExtractResult(
        vertices=m["vertices"].cuda(), faces=m["faces"].cuda(),
        vertex_attrs=None if m["vertex_attrs"] is None else m["vertex_attrs"].cuda(), res=m["res"])
    gauss = Gaussian(**raw["gaussian"]["init_params"])
    for k in GAUSSIAN_KEYS:
        setattr(gauss, k, raw["gaussian"][k].cuda())
    return mesh, gauss


def save_sample(sample: dict, name: str, seed: int, stages: list, result: dict) -> dict | None:
    """SF14 (--sample-dir): one generated sample as <dir>/<name>_seed<seed>.pt (the --dump format, read back by
    --from-dump with weights_only=True) -> {"seed", "file", "bytes"}. Written to a temporary name first, so that an
    interrupted run never leaves a truncated file under the final name.

    Saving is optional: when it fails (disk full, ...) the generation goes on without this sample (UA2-R3). The partial
    files are removed, "[sample] WARNING: seed N not saved (...)" is printed, the report lists the seed under
    sample_save_failed, and None is returned (the sample is not in saved_samples)."""
    path = ARGS.sample_dir / f"{name}_seed{seed}.pt"
    tmp = path.with_name(path.name + ".tmp")
    try:
        ARGS.sample_dir.mkdir(parents=True, exist_ok=True)
        with Stage(stages, f"save sample seed {seed}"):
            torch.save(sample, tmp)
            os.replace(tmp, path)
        size = path.stat().st_size
    except (OSError, RuntimeError) as e:
        for p in (tmp, path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        # strerror only (no path: the output goes to the app's logs)
        reason = f"{type(e).__name__}: {e.strerror if isinstance(e, OSError) and e.strerror else e}"
        result.setdefault("sample_save_failed", []).append({"seed": seed, "error": reason[:300]})
        print(f"[sample] WARNING: seed {seed} not saved ({reason[:300]})", flush=True)
        return None
    print(f"[sample] seed {seed}: {path.name}", flush=True)
    return {"seed": seed, "file": path.name, "bytes": size}


def background_removed(image: Image.Image) -> bool:
    """Whether TRELLIS preprocess_image removes the background (D-020): it keeps the image as is only
    when the mode is RGBA and some alpha is below 255."""
    return not (image.mode == "RGBA" and not np.all(np.asarray(image)[:, :, 3] == 255))


def multi_image_sampler(pipeline, sampler_name: str, count: int, overrides: dict):
    """run_multi_image's sampler injection for several images; nothing for one image. The steps are the ones the
    sampler runs (overrides from --ss-steps / --slat-steps, else pipeline.json)."""
    if count < 2:
        return contextlib.nullcontext()
    steps = gen_helpers.multi_image_steps(getattr(pipeline, sampler_name.replace("_sampler", "_sampler_params")), overrides)
    return pipeline.inject_sampler_multi_image(sampler_name, count, steps, mode=ARGS.multi_mode)


# SF15: what --low-vram moves to the CPU while decoding (the decoders stay on the GPU)
LOW_VRAM_MODELS = ("image_cond_model", "sparse_structure_flow_model", "slat_flow_model", "sparse_structure_decoder")


def low_vram_modules(pipeline) -> list:
    """The conditioning model (DINOv2 in pipeline.models, CLIP in text_cond_model) and the sampling models."""
    out = [pipeline.models[k] for k in LOW_VRAM_MODELS if k in pipeline.models]
    tcm = getattr(pipeline, "text_cond_model", None)
    if isinstance(tcm, dict) and tcm.get("model") is not None:
        out.append(tcm["model"])
    return out


def move_low_vram_modules(pipeline, device: str) -> None:
    for m in low_vram_modules(pipeline):
        m.to(device)
    torch.cuda.empty_cache()     # give the freed blocks back, so the decoders' peak is lower device-wide too


def empty_message() -> str:
    """The stop when nothing was generated (EMPTY_MARKER first in both languages: the app matches it)."""
    return gen_helpers.tr(f"{gen_helpers.EMPTY_MARKER} (the sample has no surface). Try another seed, image or text.\n"
                          "エラー: 物体が生成されませんでした（空の結果）。別のシード・画像・文章で試してください。",
                          f"{gen_helpers.EMPTY_MARKER} (the sample has no surface). Try another seed, image or text.")


def pose_checks(sample: dict) -> tuple[dict, dict, list[str]]:
    """pose_check as generated (front assumed +Z) and turned 180 deg about the vertical axis (B-004: TRELLIS
    sometimes faces -Z). Selection uses the first; the second only records whether the result depends on it."""
    a = pose_check.analyze_raw(sample, target_deg=ARGS.target_arm_angle)
    a180 = pose_check.analyze_raw(sample, target_deg=ARGS.target_arm_angle, turn_180=True)
    return a, a180, gen_helpers.orientation_disagreement(a, a180)


def select_by_pose(job: dict, name: str, out: Path, stages: list, result: dict, generate_one) -> dict:
    """D-012 / D-013: generate candidates with consecutive seeds and keep the one whose arm angle is
    closest to the bind pose, skipping candidates with weapon-like objects or no usable arm estimate.
    Empty samples (nothing generated) are skipped and never chosen; when every candidate is empty the
    job stops with empty_message().

    Writes <name>_selection.json (every candidate's estimates and why the winner was chosen) and
    <name>_candidates.png (front views). Returns the chosen raw sample.
    """
    from PIL import ImageDraw

    cands = []
    for round_index in range(ARGS.max_rounds):
        for i in range(ARGS.candidates):
            seed = job["seed"] + round_index * ARGS.candidates + i
            sample, run_info = generate_one(seed, f"seed {seed}")
            a = a180 = None
            turned: list[str] = []
            if sample is not None:
                with Stage(stages, f"pose_check seed {seed}"):
                    a, a180, turned = pose_checks(sample)
            excluded = gen_helpers.exclusions(a, ARGS.allow_weapons, run_info.get("empty"))
            cands.append({"seed": seed, "round": round_index + 1, "raw": sample, "run": run_info,
                          "analysis": a, "turned": a180, "orientation_differs": turned, "excluded": excluded})
            if ARGS.dump_dir and sample is not None:
                ARGS.dump_dir.mkdir(parents=True, exist_ok=True)
                torch.save(sample, ARGS.dump_dir / f"{name}_seed{seed}.pt")
            if ARGS.sample_dir and sample is not None:      # SF14: every candidate, so that any of them can be used later
                cands[-1]["sample"] = save_sample(sample, name, seed, stages, result)
            if a is None:
                print(f"[candidate] seed {seed}: empty result ({run_info.get('empty')}), excluded", flush=True)
            else:
                print(f"[candidate] seed {seed}: arm {a['arm_angle_deg']} deg ({a['pose_class']}), "
                      f"weapon {a['weapon_suspected']} {a['weapon_reasons']}, excluded {excluded}"
                      + (f", orientation-dependent: {'; '.join(turned)}" if turned else ""), flush=True)
        if any(not c["excluded"] for c in cands):
            break

    chosen, warning = gen_helpers.choose_candidate(cands, ARGS.allow_weapons)
    keys = ("arm_angle_deg", "arm_angle_error_deg", "pose_class", "pose_reliable", "weapon_suspected",
            "weapon_reasons", "weapon_cues")
    report = {
        "rule": (f"exclude empty samples, candidates with a weapon-like object (D-013) or without a reliable arm "
                 f"estimate; choose the smallest |arm angle - {ARGS.target_arm_angle} deg| (D-012); ties -> lower seed; "
                 f"if every candidate is excluded after {ARGS.max_rounds} round(s): prefer a weapon-free candidate "
                 f"(any candidate with --allow-weapons; even without an arm estimate), otherwise the closest arm "
                 f"angle; the warning field says which. "
                 f"The checks assume the front faces +Z; orientation_differs lists what changes when the sample is "
                 f"turned 180 deg (not used for the choice)"),
        "target_arm_angle_deg": ARGS.target_arm_angle,
        "candidates_per_round": ARGS.candidates,
        "rounds_used": max(c["round"] for c in cands),
        "chosen_seed": chosen["seed"] if chosen else None,
        "warning": warning,
        "chosen_orientation_differs": chosen["orientation_differs"] if chosen else None,
        "prompt_used": result["info"].get("prompt_used"),
        "candidates": [{"seed": c["seed"], "round": c["round"], "chosen": c is chosen, "excluded": c["excluded"],
                        "empty": c["run"].get("empty"),
                        **{k: (c["analysis"] or {}).get(k) for k in keys},
                        "sides": (c["analysis"] or {}).get("sides"),
                        "turned_180": gen_helpers.turned_summary(c["turned"]) if c["turned"] else None,
                        "orientation_differs": c["orientation_differs"],
                        "generation_seconds": c["run"]["generation_seconds"],
                        **({"sample": (c.get("sample") or {}).get("file")} if ARGS.sample_dir else {})}
                       for c in cands],
    }
    (out / f"{name}_selection.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    tiles = []
    for c in cands:
        a = c["analysis"]
        if c["raw"] is None:          # empty: nothing to render (front_preview needs faces)
            im = Image.new("RGB", (320, 320), (230, 230, 230))
        else:
            im = Image.fromarray(pose_check.front_preview(c["raw"], 320))
        d = ImageDraw.Draw(im)
        d.text((4, 4), f"seed {c['seed']}  " + (f"arm {a['arm_angle_deg']}" if a else "empty"), fill=(0, 0, 0))
        label = "CHOSEN" if c is chosen else ("excluded: " + "; ".join(c["excluded"]))[:52] if c["excluded"] else ""
        d.text((4, 18), label, fill=(0, 120, 0) if c is chosen else (180, 0, 0))
        tiles.append(im)
    sheet = Image.new("RGB", (320 * len(tiles), 320), "white")
    for i, t in enumerate(tiles):
        sheet.paste(t, (i * 320, 0))
    sheet.save(out / f"{name}_candidates.png")
    if chosen is None:
        sys.exit(f"{empty_message()}\n(every candidate of seeds {cands[0]['seed']}-{cands[-1]['seed']} was empty)")
    result["runs"].append(chosen["run"])
    if ARGS.sample_dir:
        result["saved_samples"] = [c["sample"] for c in cands if c.get("sample")]
    result["selection"] = {k: v for k, v in report.items() if k != "candidates"}
    result["pose_check"] = chosen["analysis"]
    result["pose_check_turned_180"] = gen_helpers.turned_summary(chosen["turned"])
    result["pose_orientation_differs"] = chosen["orientation_differs"]
    print(f"[select] {name}: seed {chosen['seed']} (arm {chosen['analysis']['arm_angle_deg']} deg)"
          + (f" WARNING: {warning}" if warning else ""), flush=True)
    return chosen["raw"]


def build_jobs() -> list[dict]:
    if ARGS.batch:
        jobs = json.loads(ARGS.batch.read_text(encoding="utf-8"))
        for j in jobs:
            j.setdefault("seed", ARGS.seed)
            j["kind"] = "text"
        return jobs
    if ARGS.prompt is not None:
        return [{"kind": "text", "name": ARGS.name, "prompt": ARGS.prompt, "seed": ARGS.seed}]
    if ARGS.image is not None:
        return [{"kind": "image", "name": ARGS.name or ARGS.image[0].stem, "images": [str(p) for p in ARGS.image],
                 "labels": ARGS.image_label or [None] * len(ARGS.image), "seed": ARGS.seed}]
    return [{"kind": "dump", "name": ARGS.name or ARGS.from_dump.stem, "dump": str(ARGS.from_dump)}]


def main() -> int:
    out = ARGS.out
    out.mkdir(parents=True, exist_ok=True)
    jobs = build_jobs()
    kind = jobs[0]["kind"]
    gpu = torch.cuda.get_device_properties(0)
    info = {
        "input_kind": kind,
        "postprocess": ARGS.postprocess,
        "env_root": str(ENV),
        "python": sys.prefix,
        "absent_modules": [m for m in TRELLIS_TO_GLB_MODULES if importlib.util.find_spec(m) is None],
        "env": {k: os.environ.get(k) for k in ("ATTN_BACKEND", "SPARSE_ATTN_BACKEND", "SPARSE_BACKEND", "SPCONV_ALGO",
                                               "TORCH_HOME", "HF_HOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE",
                                               "WARP_CACHE_PATH", "U2NET_HOME")},
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "gpu": gpu.name,
        "gpu_total_mib": round(gpu.total_memory / 2**20),
        "nvml_baseline_used_mib": round(nvml_used_mib(), 1),
    }
    if ARGS.postprocess == "alt":
        info["alt_options"] = {"texture_source": ARGS.texture_source, "fill_holes_views": ARGS.fill_holes_views,
                               "fill_holes_resolution": ARGS.fill_holes_resolution}
    # optional modes are recorded only when used (a default run's report is unchanged)
    if SS_PARAMS or SLAT_PARAMS:
        info["sampler_overrides"] = {"sparse_structure": SS_PARAMS, "slat": SLAT_PARAMS}
    if ARGS.low_vram:
        info["low_vram"] = True
    if ARGS.strict_offline:
        info["strict_offline"] = True
    print(json.dumps(info, indent=1, ensure_ascii=False), flush=True)   # the backend's log redaction sees the paths as written

    load_stages: list[dict] = []
    with Stage(load_stages, "import trellis"):
        from trellis.pipelines import TrellisImageTo3DPipeline, TrellisTextTo3DPipeline
        import trellis.models as trellis_models
        if ARGS.postprocess == "trellis" and ARGS.variants:
            from trellis.utils import postprocessing_utils as pp
        else:
            import postprocess_alt
    # TRELLIS retries a failed local load with the bare name (a Hugging Face download, offline an error that hides
    # the real one); load the environment's files only
    trellis_models.from_pretrained = gen_helpers.local_only_from_pretrained(trellis_models.from_pretrained)

    # trellis mode: time the parts of to_glb by wrapping the module-level helpers it calls.
    sub_times: dict[str, float] = {}
    sub_counts: dict[str, object] = {}

    def timed(fn_name):
        fn = getattr(pp, fn_name)

        def wrapper(*a, **kw):
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            r = fn(*a, **kw)
            torch.cuda.synchronize()
            sub_times[fn_name] = round(time.perf_counter() - t0, 3)
            if fn_name == "postprocess_mesh":
                sub_counts["after_postprocess"] = {"vertices": int(r[0].shape[0]), "faces": int(r[1].shape[0])}
            if fn_name == "parametrize_mesh":
                sub_counts["after_xatlas"] = {"vertices": int(r[0].shape[0]), "faces": int(r[1].shape[0])}
            return r
        setattr(pp, fn_name, wrapper)

    if ARGS.postprocess == "trellis" and ARGS.variants:
        for fn_name in ("postprocess_mesh", "parametrize_mesh", "render_multiview", "bake_texture"):
            timed(fn_name)

    pipeline = None
    if kind in ("image", "text"):
        model_dir = ENV / "models" / ("TRELLIS-image-large" if kind == "image" else "TRELLIS-text-xlarge")
        info["model_dir"] = str(model_dir)
        cls = TrellisImageTo3DPipeline if kind == "image" else TrellisTextTo3DPipeline
        problems = gen_helpers.check_model_files(str(model_dir))
        if problems:
            sys.exit(gen_helpers.missing_message(model_dir.name, problems))
        if kind == "image":
            problems = gen_helpers.dinov2_problems(torch.hub.get_dir())
            if problems:
                sys.exit(gen_helpers.missing_message("TORCH_HOME/hub (DINOv2)", problems))
        with Stage(load_stages, "load pipeline (CPU)" if kind == "image" else "load pipeline (CPU; CLIP to GPU)"):
            pipeline = cls.from_pretrained(str(model_dir))
        info.update(HUB_INFO)
        if SS_PARAMS or SLAT_PARAMS:     # what the samplers run with: pipeline.json's params with the overrides
            info["sampler_params"] = {"sparse_structure": {**pipeline.sparse_structure_sampler_params, **SS_PARAMS},
                                      "slat": {**pipeline.slat_sampler_params, **SLAT_PARAMS}}
            if ARGS.variant_base is not None:   # SF10: the sparse structure comes from the base, it is not sampled
                del info["sampler_params"]["sparse_structure"]
        # the radiance-field decoder (about 164 MiB of weights) is never used here: only mesh and Gaussians are decoded
        pipeline.models.pop("slat_decoder_rf", None)
        with Stage(load_stages, "pipeline.cuda()"):
            pipeline.cuda()
        if ARGS.variant_base is not None:
            # SF10: run_variant's coords (pipeline.voxelize of the z-up base, batch index 0 in front), built once; the
            # base file is read as given (its SHA-256 is recorded, its name is not). Imported only here: a run without
            # --variant-base does not depend on it
            import variant_base
            try:
                data = ARGS.variant_base.read_bytes()
                base_v, base_f = variant_base.load_glb(io.BytesIO(data))
            except OSError as e:
                sys.exit(f"{variant_base.BAD_BASE_MARKER} ({type(e).__name__}: {e.strerror or e})")
            except variant_base.VariantBaseError as e:
                sys.exit(str(e))
            with Stage(load_stages, "variant voxelize", base_triangles=int(len(base_f))):
                vox = pipeline.voxelize(variant_base.to_open3d(base_v, base_f))
            if vox.ndim != 2 or vox.shape[0] == 0:
                sys.exit(f"{variant_base.BAD_BASE_MARKER} (no voxel inside the grid)")
            VARIANT["coords"] = torch.cat([torch.zeros(vox.shape[0], 1, dtype=torch.int32, device=vox.device), vox], 1)
            info["variant"] = {**variant_base.describe(data, base_v, base_f), "active_voxels": int(vox.shape[0]),
                               "method": "TrellisTextTo3DPipeline.run_variant (steps inlined)"}
            print(f"[variant] base: {len(base_v)} verts, {len(base_f)} tris, {int(vox.shape[0])} voxels, tallest axis "
                  f"{info['variant']['base_tallest_axis']}", flush=True)
            del base_v, base_f, vox

    low_vram_state = {"offloaded": False}    # --low-vram: the models stay on the CPU until the next candidate needs them
    for job_index, job in enumerate(jobs):
        name = job["name"]
        stages: list[dict] = list(load_stages) if job_index == 0 else []
        # input paths and labels stay out of the report; image jobs get input_images instead
        job_meta = {k: v for k, v in job.items() if k not in ("kind", "images", "labels")}
        result = {"info": {**info, **job_meta}, "runs": [], "variants": []}
        print(f"== job {job_index + 1}/{len(jobs)}: {name}", flush=True)

        if kind == "dump":
            with Stage(stages, "load dump"):   # plain tensors only: a dump from someone else cannot run code
                try:
                    raw = torch.load(job["dump"], map_location="cpu", weights_only=True)
                except Exception as e:  # noqa: BLE001  damaged, truncated or missing file
                    sys.exit(f"{gen_helpers.SAMPLE_BAD_MARKER} ({type(e).__name__}: {e})")
                if not isinstance(raw, dict):
                    sys.exit(f"{gen_helpers.SAMPLE_BAD_MARKER} (not a saved sample: {type(raw).__name__})")
            result["dump_meta"] = raw.get("meta")
        else:
            if kind == "image":
                cond_inputs, entries = [], []
                for k, (path, label) in enumerate(zip(job["images"], job["labels"]), start=1):
                    data = Path(path).read_bytes()          # sha256 below is of the file as given
                    image = gen_helpers.normalize_image(Image.open(io.BytesIO(data)))   # as the app's copy_image
                    removed = background_removed(image)
                    try:
                        with Stage(stages, "preprocess_image", image=k, background_removed=removed):
                            cond_inputs.append(pipeline.preprocess_image(image))
                    except ValueError as e:  # empty mask: TRELLIS takes the min of an empty bbox array
                        # "ERROR: image <k>: no object was found" first in both languages (the app matches it)
                        if removed:
                            msg = gen_helpers.tr(
                                f"ERROR: image {k}: no object was found after background removal ({e}).\n"
                                f"エラー: {k} 枚目の画像に物体が見つかりませんでした（背景の除去の後）。",
                                f"ERROR: image {k}: no object was found after background removal ({e}).")
                        else:
                            msg = gen_helpers.tr(
                                f"ERROR: image {k}: no object was found (the image is fully transparent) ({e}).\n"
                                f"エラー: {k} 枚目の画像に物体が見つかりませんでした（画像全体が透明です）。",
                                f"ERROR: image {k}: no object was found (the image is fully transparent) ({e}).")
                        sys.exit(msg)
                    fname = f"{name}_preprocessed.png" if k == 1 else f"{name}_preprocessed_{k}.png"
                    cond_inputs[-1].save(out / fname)
                    entries.append({"index": k, "label": label, "background_removed": removed,
                                    "sha256": hashlib.sha256(data).hexdigest(), "preprocessed": fname})
                result["input_images"] = {"count": len(entries), "images": entries}
                result["multi_mode"] = ARGS.multi_mode if len(entries) > 1 else None
                job_meta.update(input_images=result["input_images"], multi_mode=result["multi_mode"])
            else:
                # D-013 "no weapon" kept inside CLIP's 77 tokens; prompts that fit are unchanged
                tok = pipeline.text_cond_model["tokenizer"]
                used, prompt_info = prompt_fit.fit_prompt(
                    job["prompt"], lambda s: len(tok(s, truncation=False, verbose=False)["input_ids"]),
                    add_suffix=not ARGS.allow_weapons)
                cond_inputs = [used]
                result["info"]["prompt_used"] = used
                result["info"].update(prompt_info)
                if prompt_info["prompt_truncated"]:     # no prompt text in the output (the app's logs)
                    pt, lim = prompt_info["prompt_tokens"], prompt_info["token_limit"]
                    # with_suffix: the suffix was added and the user's text shortened to make room for it (not with
                    # --allow-weapons, nor when the user's own "no weapon" is read). When the user's text alone fits,
                    # only the suffix goes over: report the tokens with it (the provider's _PROMPT_RE reads both forms)
                    shortened = "with_suffix" in pt
                    head = (f"{pt['with_suffix']} tokens with the no-weapon suffix"
                            if shortened and pt["original"] <= lim else f"{pt['original']} tokens")
                    print(f"[prompt] WARNING: {head}; CLIP reads {lim}, the end of the prompt is not used"
                          + (" (shortened to keep 'no weapon')" if shortened else ""),
                          flush=True)

            def generate_one(seed: int, run_label) -> tuple[dict | None, dict]:
                """-> (raw sample, run_info); (None, run_info with "empty") when nothing was generated."""
                run_start = len(stages)
                n = len(cond_inputs)
                if low_vram_state["offloaded"]:      # SF15: back to the GPU for this candidate
                    with Stage(stages, "low_vram restore", run=run_label):
                        move_low_vram_modules(pipeline, "cuda")
                    low_vram_state["offloaded"] = False
                with torch.no_grad():
                    with Stage(stages, "get_cond (DINOv2)" if kind == "image" else "get_cond (CLIP)", run=run_label):
                        cond = pipeline.get_cond(cond_inputs)
                    if n > 1:
                        cond["neg_cond"] = cond["neg_cond"][:1]  # as run_multi_image
                    torch.manual_seed(seed)
                    if VARIANT:          # SF10: run_variant keeps the base's voxels; only the structured latent is sampled
                        coords = VARIANT["coords"]
                    else:
                        with Stage(stages, "sample_sparse_structure", run=run_label):
                            with multi_image_sampler(pipeline, "sparse_structure_sampler", n, SS_PARAMS):
                                coords = pipeline.sample_sparse_structure(cond, 1, SS_PARAMS)
                    if coords.shape[0] == 0:     # no occupied voxel: sample_slat cannot build an empty SparseTensor
                        run_info = {"run": run_label, "seed": seed, "active_voxels": 0, "raw_mesh_vertices": 0,
                                    "raw_mesh_faces": 0, "gaussians": 0, "empty": "no_sparse_structure",
                                    "generation_seconds": round(sum(st["seconds"] for st in stages[run_start:]), 3)}
                        del cond, coords
                        torch.cuda.empty_cache()
                        return None, run_info
                    with Stage(stages, "sample_slat", run=run_label, active_voxels=int(coords.shape[0])):
                        with multi_image_sampler(pipeline, "slat_sampler", n, SLAT_PARAMS):
                            slat = pipeline.sample_slat(cond, coords, SLAT_PARAMS)
                    if ARGS.low_vram:                # SF15: only the decoders are needed from here
                        with Stage(stages, "low_vram offload", run=run_label):
                            move_low_vram_modules(pipeline, "cpu")
                        low_vram_state["offloaded"] = True
                    with Stage(stages, "decode_slat mesh", run=run_label):
                        mesh = pipeline.models["slat_decoder_mesh"](slat)
                    with Stage(stages, "decode_slat gaussian", run=run_label):
                        gauss = pipeline.models["slat_decoder_gs"](slat)
                run_info = {
                    "run": run_label, "seed": seed,
                    "active_voxels": int(coords.shape[0]),
                    "raw_mesh_vertices": int(mesh[0].vertices.shape[0]),
                    "raw_mesh_faces": int(mesh[0].faces.shape[0]),
                    "gaussians": int(gauss[0].get_xyz.shape[0]),
                    "generation_seconds": round(sum(st["seconds"] for st in stages[run_start:]), 3),
                }
                sample = None
                if not mesh[0].success or run_info["raw_mesh_faces"] == 0:   # FlexiCubes found no surface
                    run_info["empty"] = "no_surface"
                else:
                    sample = sample_to_raw(mesh[0], gauss[0], {**job_meta, "seed": seed,
                                                               "prompt_used": result["info"].get("prompt_used"),
                                                               **run_info})
                del cond, coords, slat, mesh, gauss
                torch.cuda.empty_cache()
                return sample, run_info

            if ARGS.select_pose:
                raw = select_by_pose(job, name, out, stages, result, generate_one)
            else:
                for run in range(1, ARGS.runs + 1):
                    raw, run_info = generate_one(job["seed"], run)
                    result["runs"].append(run_info)
                if raw is None:
                    sys.exit(empty_message())
                if ARGS.sample_dir:                                 # SF14: the one sample (the last run's)
                    saved = save_sample(raw, name, job["seed"], stages, result)
                    result["saved_samples"] = [saved] if saved else []
                a, a180, turned = pose_checks(raw)                  # recorded only
                result["pose_check"] = a
                result["pose_check_turned_180"] = gen_helpers.turned_summary(a180)
                result["pose_orientation_differs"] = turned
            dump_path = ARGS.dump if (ARGS.dump and len(jobs) == 1) else (ARGS.dump_dir / f"{name}.pt" if ARGS.dump_dir else None)
            if dump_path:
                dump_path.parent.mkdir(parents=True, exist_ok=True)
                torch.save(raw, dump_path)
                print(f"[dump] {dump_path}", flush=True)
            if job_index == len(jobs) - 1:
                del pipeline  # free the models before the last post-processing
                pipeline = None
                torch.cuda.empty_cache()

        if ARGS.postprocess == "trellis" and ARGS.variants:
            mesh0, gauss0 = raw_to_sample(raw)

        for spec in ARGS.variants:
            simplify, tex = spec.split(":")
            simplify, tex = float(simplify), int(tex)
            tag = f"s{simplify:g}_t{tex}"
            sub_times.clear()
            sub_counts.clear()
            if ARGS.postprocess == "trellis":
                path = out / f"{name}_trellis_{tag}.glb"
                with Stage(stages, f"to_glb (trellis) {tag}", simplify=simplify, texture_size=tex):
                    glb = pp.to_glb(gauss0, mesh0, simplify=simplify, texture_size=tex, verbose=False)
            else:
                path = out / f"{name}_{tag}.glb"
                with Stage(stages, f"to_glb_alt {tag}", simplify=simplify, texture_size=tex):
                    glb = postprocess_alt.to_glb_alt(
                        raw, simplify, tex, ARGS.texture_source,
                        fill_holes_views=ARGS.fill_holes_views, fill_holes_resolution=ARGS.fill_holes_resolution,
                        times=sub_times, counts=sub_counts)
            with Stage(stages, f"export {tag}"):
                glb.export(str(path))
            stats = glb_stats(path)
            stats.update({"postprocess": ARGS.postprocess, "simplify": simplify, "texture_size": tex, "file": path.name,
                          "parts_seconds": dict(sub_times), "intermediate_counts": dict(sub_counts),
                          "under_65535_vertices": stats["vertices"] <= 65535})
            result["variants"].append(stats)
            print(f"[glb] {path.name}: {stats['vertices']} verts, {stats['triangles']} tris, "
                  f"tex {stats['textures'][0]['size'] if stats['textures'] else None}, "
                  f"extent {stats['extent']}, parts {sub_times}", flush=True)
            if not ARGS.no_views:
                render_views(path, path.with_name(path.stem + "_views.png"))

        result["stages"] = stages
        result["process_peak"] = process_peak(stages) if stages else None
        if ARGS.strict_offline:          # SF17: connections refused so far (none in a normal run)
            result["offline_blocked"] = list(OFFLINE_BLOCKED)
        (out / f"{name}_metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result["process_peak"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
