"""自前の透明の見た目（2026-11-11。D-079）: 自前の透明の肩・メダルが使うメッシュと画像。

公式の透明の肩・メダルは、メッシュ ``system/meshes/invisible01.msh``（三角形がいくつかあるだけの静的なメッシュで、貼る画像は
``System/textures/invisible.tex``）と、全透明の 32x32 の画像 ``system/textures/invisible.tex`` で見えなくしている
（2026-11-11 にこの PC のゲームデータで確認）。自前の肩・メダルが同じパスのままだと、公式の透明の肩・メダルを収集済みの
セーブでは、消耗品を使っても収集に入らなかった（利用者のゲーム内の確認と、セーブの収集の記録の読み取り。ゲームが同じ見た目の
イリュージョンを足さないためと見ている [推定]）。そこで、見た目は同じでパスだけが違うファイルを MOD の Items.arc に入れる。

* 画像 ``items/aiskin/invisible/invisible.tex``: アプリが描く全透明の 32x32（:func:`modbuilder.icons.tex_bytes` の形。
  公式の ``system/textures/invisible.tex`` とバイト単位で同じになることを 2026-11-11 に確認）。
* メッシュ ``items/aiskin/invisible/invisible.msh``: MOD の作成のときに、この PC のゲームの公式の透明メッシュを読み、
  マテリアルの貼る画像だけを上の画像（``Items/aiskin/invisible/invisible.tex``）に替えたもの（公式のレコードの差分コピーと
  同じく、利用者のゲームのデータから作る。gdmesh で厳密に読み書きでき、書き戻しがバイト単位で同じことを確認）。

これでゲームが別のイリュージョンとして収集に入れるかは、ゲーム内では未確認。
"""
from __future__ import annotations

import os

import numpy as np

from ._i18n import tr
from .assets import retarget_textures
from .errors import ModBuilderError
from .icons import tex_bytes
from .naming import (INVISIBLE_LOOK_MSH, INVISIBLE_LOOK_MSH_TEXTURE, INVISIBLE_LOOK_TEX, OFFICIAL_INVISIBLE_TEX,
                     SYSTEM_INVISIBLE_MESH)

__all__ = ["LOOK_FILES", "look_tex", "look_msh", "look_files", "write_look"]

LOOK_FILES = (INVISIBLE_LOOK_TEX, INVISIBLE_LOOK_MSH)


def look_tex() -> bytes:
    """全透明の 32x32 の画像（公式の system/textures/invisible.tex と同じ形）。"""
    return tex_bytes(np.zeros((32, 32, 4), dtype=np.uint8))


def look_msh(gd) -> bytes:
    """この PC のゲームの公式の透明メッシュの、貼る画像（System/textures/invisible.tex）だけを自前の画像に替えたもの。

    公式のメッシュが読めないときは GameDataError（gd.read_official_file）。形が想定と違う（読み書きが一致しない・
    透明の画像を貼るマテリアルが 1 つでない）ときは ModBuilderError。"""
    data = gd.read_official_file(SYSTEM_INVISIBLE_MESH)
    try:
        out, n = retarget_textures(data, {OFFICIAL_INVISIBLE_TEX: INVISIBLE_LOOK_MSH_TEXTURE})
    except Exception as e:  # noqa: BLE001  gdmesh の例外は種類が多い
        raise ModBuilderError(tr(f"公式の透明メッシュ {SYSTEM_INVISIBLE_MESH} を読み書きできません: {type(e).__name__}: {e}",
                                 f"Cannot read and rewrite the official invisible mesh {SYSTEM_INVISIBLE_MESH}: "
                                 f"{type(e).__name__}: {e}")) from e
    if n != 1:
        raise ModBuilderError(tr(f"公式の透明メッシュ {SYSTEM_INVISIBLE_MESH} の形が想定と違います"
                                 f"（{OFFICIAL_INVISIBLE_TEX} を貼るところが {n} か所）",
                                 f"The official invisible mesh {SYSTEM_INVISIBLE_MESH} is not in the expected form "
                                 f"({OFFICIAL_INVISIBLE_TEX} is used in {n} places)"))
    return out


def look_files(gd) -> dict[str, bytes]:
    """{ゲーム内のパス: 中身}。"""
    return {INVISIBLE_LOOK_TEX: look_tex(), INVISIBLE_LOOK_MSH: look_msh(gd)}


def write_look(items_root: str, gd) -> list[str]:
    """Items の作業フォルダー（``staging/resources/Items``）に書く（ゲーム内のパス ``items/x/y`` → ``<items_root>/x/y``。
    アイコン・装備のアセットと同じ置き方）。書いたゲーム内のパスの一覧を返す。"""
    files = look_files(gd)
    for p, data in files.items():
        dst = os.path.join(items_root, *p.split("/")[1:])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data)
    return sorted(files)
