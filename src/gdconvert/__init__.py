"""gdconvert: AI 生成 3D モデル（GLB）を Grim Dawn の頭スロット用の全身スキンメッシュに変換する。

モジュール（計画書 6 章の名前との対応）:

* :mod:`gdconvert.gltf`      GLB / glTF の読み込み
* :mod:`gdconvert.meshproc`  MeshProcessor（向き、掃除、頂点数の自動調整 D-009）
* :mod:`gdconvert.rigging`   RiggingProcessor（基準ボディへの合わせ込み、ウェイト転写、バインド姿勢への戻し）
* :mod:`gdconvert.texture`   TextureProcessor（2 の累乗化、焼き直し、余白、TextureCompiler で .tex）
* :mod:`gdconvert.exporter`  GrimDawnExporter（gdmesh でスキンメッシュを組み立てて検証）
* :mod:`gdconvert.pipeline`  全工程（:func:`gdconvert.pipeline.convert`）
* :mod:`gdconvert.cli`       コマンドライン（``python -m gdconvert``）

依存: numpy, scipy (BSD-3), Pillow (MIT-CMU), fast-simplification (MIT), xatlas (MIT), gdmesh（同梱）。
"""
__version__ = "0.1.0"
