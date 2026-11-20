"""3D 生成 AI の取り決め（計画書 基本方針 8、docs/app_architecture.md 10 章）。

生成 AI はそれぞれ別の Python 環境・別のコマンドで動く前提で、バックエンドは子プロセスとして起動する。
新しい生成 AI を足すときは :class:`GenerationProvider` を継承したクラスを書き、``providers/__init__.py`` の
``REGISTRY`` に登録する。

出力の取り決め: GLB 1 つ（y 軸が上、正面 +Z、ベースカラーのテクスチャ 1 枚）。向きが違う場合は
:attr:`GenerationOutput.axes` に ``{"source_up": "+z", "source_front": "-y"}`` のように返せば、変換（gdconvert の
``AxisConfig``）で揃える。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GenerationRequest:
    kind: str                       # "image" / "text"
    settings: dict                  # settings.validate_generation の結果
    image_path: str | None = None   # 下書きの input\ にコピーした画像（複数のときは 1 枚目）
    text: str | None = None
    name: str = "model"             # 出力ファイルの名前（利用者の入力を含めない）
    image_paths: list[str] | None = None    # 複数画像（1〜4 枚。1 枚目は image_path と同じ）
    image_labels: list[str] | None = None   # 各画像の名前（正面・背面など。生成には使わず、報告に残る）
    # 見た目の作り直し（SF10。kind "text" のときだけ）: 形・姿勢を保つ元の GLB（下書きの input\variant_base.glb）
    variant_base: str | None = None

    @property
    def images(self) -> list[str]:
        return list(self.image_paths) if self.image_paths else ([self.image_path] if self.image_path else [])


@dataclass
class ProviderRun:
    cmd: list[str]
    env: dict = field(default_factory=dict)
    cwd: str | None = None
    state: dict = field(default_factory=dict)      # parse_line / classify_failure / collect が共有する状態


@dataclass
class ProgressUpdate:
    progress: float | None          # 生成の中での 0〜1
    stage: str | None = None        # 画面のリソースのキー（Stage_<stage>）
    detail: dict | None = None
    log: str | None = None          # ジョブのログに出す要約（任意）


@dataclass
class GenerationOutput:
    glb: str
    model_id: str
    seed: int | None = None
    files: dict = field(default_factory=dict)      # 役割 → パス（candidates_png、preprocessed_png、metrics など）
    warnings: list[str] = field(default_factory=list)
    axes: dict | None = None
    info: dict = field(default_factory=dict)       # 記録用（ポーズの判定など）


@dataclass
class CheckItem:
    id: str
    status: str                     # ok / warning / error / missing
    code: str | None = None         # errors.CODES のどれか（ok 以外）
    message: str = ""
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "code": self.code, "message": self.message, "params": self.params}


class GenerationProvider:
    id = "base"
    display_name = "base"
    inputs: tuple[str, ...] = ()

    def describe(self, settings: dict) -> dict:
        """画面に出す情報: 入力の種類、モデル、設定項目。"""
        raise NotImplementedError

    def check(self, settings: dict, kind: str | None = None) -> list[CheckItem]:
        """使えるか。kind を渡すとその入力で生成できるかを見る。"""
        raise NotImplementedError

    def prepare(self, settings: dict, request: GenerationRequest, out_dir: str) -> ProviderRun:
        raise NotImplementedError

    def parse_line(self, line: str, run: ProviderRun) -> ProgressUpdate | None:
        return None

    def classify_failure(self, output: str, run: ProviderRun) -> str:
        return "GENERATION_FAILED"

    def collect(self, settings: dict, request: GenerationRequest, out_dir: str, run: ProviderRun) -> GenerationOutput:
        raise NotImplementedError
