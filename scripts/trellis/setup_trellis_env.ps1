<#
.SYNOPSIS
  Build a TRELLIS-image-large environment on Windows (RTX 50 series / sm_120, PyTorch 2.7.0 +
  CUDA 12.8). Everything is placed under -EnvRoot; nothing is installed system-wide and no
  administrator rights are needed.

.DESCRIPTION
  Two modes (D-010):

  runtime (default, equivalent to the distributed tool)
    Image -> GLB with the permissive-licence post-processing (postprocess_alt.py). Prebuilt wheels
    only. Does NOT install nvdiffrast, diff-gaussian-rasterization, pymeshfix or igraph, and needs
    neither Visual Studio Build Tools nor a CUDA Toolkit.
    Prerequisites: NVIDIA driver R570+ (CUDA 12.8), git, uv.

  dev (development / comparison)
    runtime + TRELLIS's original to_glb for comparison (generate_glb.py --postprocess trellis):
    pymeshfix and igraph (GPL), pip/wheel, and nvdiffrast + diff-gaussian-rasterization
    (non-commercial licences) compiled from source with the CUDA 12.8.1 redistributable build
    tools and Visual Studio 2022 / Build Tools 2022 (MSVC v143; CUDA 12.8 does not work with VS 2026).

  -EnvRoot (required, D-027): the folder to build in. GD Skin Studio's default is
  %LOCALAPPDATA%\GrimDawnSkinMOD\trellis-runtime; keep the path short and ASCII-only (the venv adds
  about 150 characters). With -Mode dev it must not contain & | < > ^ % ! (the CUDA extensions are built through
  cmd.exe). -UvCache and -DownloadDir default to folders inside it.

  Steps: managed CPython 3.11.9 -> venv -> PyTorch/xformers (PyTorch index) -> pinned packages
  (requirements-trellis-runtime.hashes.txt wheels only, then utils3d from
  requirements-trellis-sdist.hashes.txt [+ requirements-trellis-dev-extra.hashes.txt]) -> pinned
  sources -> [dev: CUDA build tools, build + install the two CUDA extensions] -> checks -> models
  (TRELLIS-image-large, DINOv2, and rembg's u2net.onnx at <EnvRoot>\u2net for images without
  transparency, D-020).
  PyTorch and the packages are installed with --require-hashes from the same hash files as the app's
  setup, with uv's configuration files and the user's index / build environment variables ignored
  (UV_NO_CONFIG) and the Windows certificate store trusted (UV_SYSTEM_CERTS, uv 0.11+). Unlike the
  app, utils3d is checked against the SHA-256 of GitHub's archive ZIP itself (the app checks every
  file in it), and uv comes from PATH (the app uses uv 0.11.28; another version prints a warning).
  The dev extras are installed the same way (--require-hashes, PyPI wheels only) from
  requirements-trellis-dev-extra.hashes.txt, made from requirements-trellis-dev-extra.lock.txt by
  scripts/package/pin_requirements.py --only dev-extra (audit R14B-06). Neither file is in the distribution
  ZIP: run -Mode dev from a repository checkout.
  Environment variables set or removed here are put back when the script ends, also when it is run in an
  open PowerShell (.\setup_trellis_env.ps1) and stops with an error.
  -WithText also fetches text-to-3D models (TRELLIS-text-xlarge 4.1 GB + CLIP ViT-L/14 1.7 GB). Before that it
  shows notices/clip_model_card_notice.ja.txt (-ClipNoticeLang en: clip_model_card_notice.en.txt) and asks for
  consent on the console (D-011); declining skips text-to-3D only. -AcceptClipNotice is for callers that have
  shown the same notice themselves. The consent records the language and the SHA-256 of the notice.

.EXAMPLE
  pwsh -File setup_trellis_env.ps1 -EnvRoot "$env:LOCALAPPDATA\GrimDawnSkinMOD\trellis-runtime"
  pwsh -File setup_trellis_env.ps1 -EnvRoot D:\GDSkin\trellis-runtime -WithText
  pwsh -File setup_trellis_env.ps1 -Mode dev -EnvRoot D:\GDSkin\trellis-env
#>
param(
    [ValidateSet('runtime', 'dev')]
    [string]$Mode = 'runtime',
    [string]$EnvRoot = '',
    [string]$UvCache = '',
    [string]$DownloadDir = '',
    [switch]$SkipModels,
    [switch]$WithText,
    [switch]$AcceptClipNotice,
    [ValidateSet('ja', 'en')]
    [string]$ClipNoticeLang = 'ja'
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$dev = $Mode -eq 'dev'

# Pinned sources
$TRELLIS_URL = 'https://github.com/microsoft/TRELLIS.git'
$TRELLIS_COMMIT = '442aa1e1afb9014e80681d3bf604e8d728a86ee7'      # 2025-11-05, FlexiCubes submodule 815e075
$NVDIFFRAST_URL = 'https://github.com/NVlabs/nvdiffrast.git'
$NVDIFFRAST_COMMIT = '253ac4fcea7de5f396371124af597e6cc957bfae'   # tag v0.4.0
$MIPSPLAT_URL = 'https://github.com/autonomousvision/mip-splatting.git'
$MIPSPLAT_COMMIT = 'dda02ab5ecf45d6edb8c540d9bb65c7e451345a9'     # submodules/diff-gaussian-rasterization
$TORCH_INDEX = 'https://download.pytorch.org/whl/cu128'
$KAOLIN_LINKS = 'https://nvidia-kaolin.s3.us-east-2.amazonaws.com/torch-2.7.0_cu128.html'
$UV_VERSION = '0.11.28'      # the version the app's setup uses (setup_manifest.json)

if (-not $EnvRoot) {
    throw ("-EnvRoot is required: the folder to build the TRELLIS environment in, e.g. " +
           "-EnvRoot `"$env:LOCALAPPDATA\GrimDawnSkinMOD\trellis-runtime`"")
}
$EnvRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EnvRoot)
# A trailing \ (tab completion adds one) is dropped: Windows PowerShell 5.1 passes 'D:\GD Skin\rt\' to fetch_models.py
# as "D:\GD Skin\rt\" and Python reads the \" as a quote in the path (audit R14B-04). A drive root such as D:\ keeps it.
if ($EnvRoot.Length -gt 3) { $EnvRoot = $EnvRoot.TrimEnd('\', '/') }
# -Mode dev passes paths in EnvRoot to build_cuda_ext.cmd, and cmd.exe splits or expands an argument with these
# characters (with C:\R&D\... it ran 'D\...' as another command): stop before anything is fetched (audit R14A-02)
if ($dev -and $EnvRoot -match '[&|<>^%!"]') {
    throw ("-EnvRoot must not contain any of & | < > ^ % ! with -Mode dev (the CUDA extensions are built by cmd.exe): " +
           "$EnvRoot - use a short ASCII path without them")
}
if (-not $UvCache) { $UvCache = Join-Path $EnvRoot 'uv-cache' }
if (-not $DownloadDir) { $DownloadDir = Join-Path $EnvRoot 'downloads' }
# $env: changes the environment of this PowerShell process: run as .\setup_trellis_env.ps1 in an open console,
# the settings below would stay there after the script (the user's index variables removed, the uv cache and
# HF_HOME in EnvRoot), so they are put back when the script ends or stops (audit R14A-03)
$cleared = @('VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONPATH', 'PYTHONHOME', 'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL',
             'PIP_FIND_LINKS', 'PIP_REQUIRE_VIRTUALENV', 'UV_INDEX', 'UV_INDEX_URL', 'UV_EXTRA_INDEX_URL',
             'UV_DEFAULT_INDEX', 'UV_FIND_LINKS', 'UV_PYTHON', 'UV_SYSTEM_PYTHON', 'UV_NO_BUILD_ISOLATION',
             'UV_REQUIRE_HASHES', 'UV_LINK_MODE', 'UV_OFFLINE', 'UV_NO_INDEX', 'UV_CONSTRAINT', 'UV_OVERRIDE',
             'UV_PRERELEASE', 'UV_INDEX_STRATEGY', 'UV_KEYRING_PROVIDER', 'UV_TOOL_DIR',
             # the models are fetched here: offline mode, and hf_transfer which is not in the pinned packages, would
             # stop fetch_models.py (it removes them too; audit R14A-03, R14B-02)
             'HF_HUB_OFFLINE', 'HF_HUB_ENABLE_HF_TRANSFER')
# Every other UV_* of the user is removed too, as in the app (UV_CONFIG_FILE is read even with UV_NO_CONFIG, and
# UV_NO_MANAGED_PYTHON conflicts with --python-preference; audit R11B-01). Proxy and certificate variables
# (HTTPS_PROXY, SSL_CERT_FILE, ...) do not start with UV_ and are kept.
$cleared += @(Get-ChildItem Env: | Where-Object { $_.Name -like 'UV_*' -and $cleared -notcontains $_.Name } |
              ForEach-Object { $_.Name })
$savedEnv = @{}
foreach ($name in $cleared + @('UV_NO_CONFIG', 'UV_SYSTEM_CERTS', 'UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR', 'TRELLIS_ENV',
                              'WARP_CACHE_PATH', 'TRELLIS_SRC', 'TRELLIS_SCRIPTS', 'TRELLIS_PROFILE', 'HF_HOME',
                              'HF_HUB_DISABLE_TELEMETRY', 'HF_HUB_DISABLE_SYMLINKS_WARNING')) {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    # Same isolation as the app's setup (gdskin_backend/setup _uv_env, audit C36): no uv.toml / user configuration,
    # none of the user's index, build or virtual-environment variables, and the Windows certificate store trusted in
    # addition to uv's bundled roots (HTTPS-inspecting antivirus / corporate proxies; audit C08). The packages stay
    # pinned by --require-hashes.
    foreach ($name in $cleared) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    $env:UV_NO_CONFIG = '1'          # set after the removal: $cleared holds the user's own UV_NO_CONFIG etc. too
    $env:UV_SYSTEM_CERTS = 'true'
    $env:UV_CACHE_DIR = $UvCache
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $EnvRoot 'python'
    $venv = Join-Path $EnvRoot 'venv'
    $py = Join-Path $venv 'Scripts\python.exe'
    $src = Join-Path $EnvRoot 'src'
    $wheels = Join-Path $EnvRoot 'wheels'

    function Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
    function Exec([string]$exe, [string[]]$argList) {
        & $exe @argList
        if ($LASTEXITCODE -ne 0) { throw "$exe $($argList -join ' ') failed with exit code $LASTEXITCODE" }
    }
    function Get-Source([string]$url, [string]$dir, [string]$commit, [switch]$Submodules) {
        # Shallow fetch of the pinned commit only (the full TRELLIS history is about 1.1 GB).
        # -LiteralPath: a [ ] in -EnvRoot is not a wildcard (otherwise a run again added origin twice; audit R14B-03)
        if (-not (Test-Path -LiteralPath (Join-Path $dir '.git'))) {
            Exec git @('init', '--quiet', $dir)
            Exec git @('-C', $dir, 'remote', 'add', 'origin', $url)
        }
        Exec git @('-C', $dir, 'fetch', '--quiet', '--depth', '1', 'origin', $commit)
        Exec git @('-C', $dir, 'checkout', '--quiet', '--force', $commit)
        if ($Submodules) { Exec git @('-C', $dir, 'submodule', 'update', '--init', '--recursive', '--depth', '1') }
    }
    function Build-CudaExtension([string]$name, [string]$srcDir, [string]$outDir) {
        # The build's stderr goes to the log as text and only the exit code decides: with 'Stop', Windows PowerShell 5.1
        # made each redirected stderr line of a native command an error and stopped at the first one (a pip notice, a
        # compiler warning) with the log cut (audit R14A-01). The preference set here ends with the function.
        # -LiteralPath: a [ ] in -EnvRoot is not a wildcard (audit R14B-03)
        $log = Join-Path $outDir "build_$name.log"
        $ErrorActionPreference = 'Continue'
        & (Join-Path $here 'build_cuda_ext.cmd') $srcDir $outDir 2>&1 | ForEach-Object { "$_" } |
            Out-File -LiteralPath $log -Encoding utf8
        if ($LASTEXITCODE -ne 0) { throw "building $name failed (exit code $LASTEXITCODE), see $log" }
    }

    Step "Mode: $Mode, EnvRoot: $EnvRoot"
    Step 'Checking prerequisites'
    foreach ($tool in 'nvidia-smi', 'git', 'uv') {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool was not found in PATH" }
    }
    $uvVersion = ((& uv --version) -split '\s+')[1]
    Write-Host "uv: $uvVersion"
    if ($uvVersion -ne $UV_VERSION) {
        Write-Warning "uv $uvVersion is not the version the app uses ($UV_VERSION); UV_SYSTEM_CERTS needs uv 0.11 or later"
    }
    # A driver too old to know compute_cap answers with an error: ask again without it (the same as the app's
    # envcheck.gpu_info), and stop with the driver requirement instead of a PowerShell error on a failed nvidia-smi
    # (audit R14B-05). All lines are read so that $LASTEXITCODE is nvidia-smi's own.
    $gpuLines = @(nvidia-smi '--query-gpu=name,driver_version,compute_cap' '--format=csv,noheader')
    if ($LASTEXITCODE -ne 0) { $gpuLines = @(nvidia-smi '--query-gpu=name,driver_version' '--format=csv,noheader') }
    $gpu = if ($gpuLines) { "$($gpuLines[0])" } else { '' }
    Write-Host "GPU: $gpu"
    $gpuParts = @($gpu -split ',')
    $driver = $null
    if ($LASTEXITCODE -ne 0 -or $gpuParts.Count -lt 2 -or -not [version]::TryParse($gpuParts[1].Trim(), [ref]$driver)) {
        throw "nvidia-smi failed (exit code $LASTEXITCODE): $($gpuLines -join ' ') - install NVIDIA driver R570 or later (CUDA 12.8)"
    }
    if ($driver.Major -lt 570) { throw "NVIDIA driver $driver is too old for CUDA 12.8 (need R570+)" }
    if ($dev) {
        $vsRequired = 'dev profile: Visual Studio 2022 / Build Tools 2022 with C++ x64 tools (MSVC v143) is required to build the CUDA extensions'
        $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
        if (-not (Test-Path -LiteralPath $vswhere)) { throw "$vsRequired (vswhere.exe of the Visual Studio Installer was not found: $vswhere)" }
        $vs = & $vswhere -latest -products * -version '[17.0,18.0)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if (-not $vs) { throw $vsRequired }
        Write-Host "VS 2022 C++ tools: $vs"
    }
    New-Item -ItemType Directory -Force $EnvRoot, $src | Out-Null

    Step 'Python 3.11.9 (uv-managed, no PATH shim, no registry entry) and venv'
    Exec uv @('python', 'install', '3.11.9', '--no-bin', '--no-registry')
    if (-not (Test-Path -LiteralPath $py)) { Exec uv @('venv', $venv, '--python', '3.11.9', '--python-preference', 'only-managed') }

    Step 'PyTorch 2.7.0+cu128, torchvision, xformers (PyTorch index, requirements-trellis-torch.hashes.txt)'
    Exec uv @('pip', 'install', '--python', $py, '--no-deps', '--require-hashes', '--index-url', $TORCH_INDEX,
        '-r', (Join-Path $here 'requirements-trellis-torch.hashes.txt'))

    Step 'Pinned packages (requirements-trellis-runtime.hashes.txt, wheels only)'
    Exec uv @('pip', 'install', '--python', $py, '--no-deps', '--require-hashes', '--only-binary', ':all:',
        '-r', (Join-Path $here 'requirements-trellis-runtime.hashes.txt'), '--find-links', $KAOLIN_LINKS)

    Step 'utils3d built from the pinned GitHub archive (requirements-trellis-sdist.hashes.txt, with the pinned setuptools)'
    Exec uv @('pip', 'install', '--python', $py, '--no-deps', '--require-hashes', '--no-build-isolation',
        '-r', (Join-Path $here 'requirements-trellis-sdist.hashes.txt'))

    Step 'TRELLIS source at the pinned commit'
    Get-Source $TRELLIS_URL (Join-Path $src 'TRELLIS') $TRELLIS_COMMIT -Submodules

    if ($dev) {
        Step 'dev: GPL packages and build tools (requirements-trellis-dev-extra.hashes.txt, wheels only)'
        Exec uv @('pip', 'install', '--python', $py, '--no-deps', '--require-hashes', '--only-binary', ':all:',
            '-r', (Join-Path $here 'requirements-trellis-dev-extra.hashes.txt'))

        Step 'dev: nvdiffrast and mip-splatting sources at pinned commits'
        Get-Source $NVDIFFRAST_URL (Join-Path $src 'nvdiffrast') $NVDIFFRAST_COMMIT
        Get-Source $MIPSPLAT_URL (Join-Path $src 'mip-splatting') $MIPSPLAT_COMMIT

        Step 'dev: CUDA 12.8.1 build tools from NVIDIA redistributable archives'
        New-Item -ItemType Directory -Force $wheels, $DownloadDir | Out-Null
        Exec $py @('-I', (Join-Path $here 'fetch_cuda_redist.py'), (Join-Path $EnvRoot 'cuda-12.8'),
            '--download-dir', (Join-Path $DownloadDir 'cuda-redist'))

        Step 'dev: building nvdiffrast and diff-gaussian-rasterization (non-commercial licenses)'
        $env:TRELLIS_ENV = $EnvRoot
        $builds = @{
            'nvdiffrast'                  = (Join-Path $src 'nvdiffrast')
            'diff_gaussian_rasterization' = (Join-Path $src 'mip-splatting\submodules\diff-gaussian-rasterization')
        }
        foreach ($name in $builds.Keys) { Build-CudaExtension $name $builds[$name] $wheels }
        Exec uv @('pip', 'install', '--python', $py, '--no-deps',
            (Join-Path $wheels 'nvdiffrast-0.4.0-cp311-cp311-win_amd64.whl'),
            (Join-Path $wheels 'diff_gaussian_rasterization-0.0.0-cp311-cp311-win_amd64.whl'))
    }

    Step 'Consistency and import check'
    Exec uv @('pip', 'check', '--python', $py)
    $env:WARP_CACHE_PATH = Join-Path $EnvRoot 'warp-cache'
    $env:TRELLIS_SRC = Join-Path $src 'TRELLIS'
    $env:TRELLIS_SCRIPTS = $here
    $env:TRELLIS_PROFILE = $Mode
    Exec $py @('-I', '-c', @'
import importlib.util, os, sys
sys.path[:0] = [os.environ['TRELLIS_SRC'], os.environ['TRELLIS_SCRIPTS']]
os.environ.setdefault('ATTN_BACKEND', 'xformers')
import torch, xformers.ops, spconv.pytorch, kaolin, utils3d, scipy, xatlas, pyvista, trimesh, cv2, open3d
assert torch.cuda.is_available(), 'CUDA is not available'
print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0))
from trellis.pipelines import TrellisImageTo3DPipeline
import postprocess_alt
v = torch.tensor([[-0.5, -0.5, 0, 1], [0.5, -0.5, 0, 1], [0, 0.5, 0, 1]], device='cuda')
fid, _ = postprocess_alt.rasterize(v, torch.tensor([[0, 1, 2]], device='cuda'), 64, 64)
assert int((fid >= 0).sum()) > 0
optional = ['nvdiffrast', 'diff_gaussian_rasterization', 'pymeshfix', 'igraph']
present = [m for m in optional if importlib.util.find_spec(m) is not None]
if os.environ['TRELLIS_PROFILE'] == 'dev':
    import nvdiffrast.torch as dr, diff_gaussian_rasterization, pymeshfix, igraph
    dr.RasterizeCudaContext()
    print('dev: original to_glb dependencies present:', present)
elif present:
    print('WARNING: runtime profile but these dev-only packages are installed:', present)
else:
    print('runtime: none of', optional, 'installed')
print('imports ok')
'@)

    if (-not $SkipModels) {
        Step ('Models (TRELLIS-image-large, DINOv2, rembg u2net' + $(if ($WithText) { ', TRELLIS-text-xlarge, CLIP' } else { '' }) + ') at pinned revisions')
        $env:HF_HOME = Join-Path $EnvRoot 'hf-home'
        $env:HF_HUB_DISABLE_TELEMETRY = '1'
        $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
        $fetchArgs = @('-I', (Join-Path $here 'fetch_models.py'), $EnvRoot)
        Exec $py $fetchArgs
        if ($WithText) {
            Step 'Text-to-3D models: CLIP model-card notice and consent (D-011)'
            $textArgs = $fetchArgs + @('--text', "--clip-notice-lang=$ClipNoticeLang")
            if ($AcceptClipNotice) { $textArgs += '--accept-clip-notice' }
            & $py @textArgs
            if ($LASTEXITCODE -ne 0) { Write-Warning 'Text-to-3D models were not fetched (notice not accepted or download failed). Image-to-3D is ready.' }
        }
    }
    Step "Done: $EnvRoot ($Mode)"
} finally {
    foreach ($name in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process') }
}
