"""gdmesh: Grim Dawn ``.msh`` メッシュの読み書きと新規作成。

* :mod:`gdmesh.msh`   - 型付きチャンクのリーダー / ライター（往復でバイト一致）
* :mod:`gdmesh.build` - 頂点配列から新規 ``.msh`` を組み立てる API
"""
from . import msh, build  # noqa: F401
from .msh import MshFile, read, read_bytes, write, to_bytes  # noqa: F401
from .build import (  # noqa: F401
    MaterialSpec, build_static_mesh, build_skinned_mesh,
    compute_normals, compute_tangents, make_box,
)

__version__ = "0.1.0"
