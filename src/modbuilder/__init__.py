"""modbuilder: 装備ライブラリから Grim Dawn の MOD 一式（database.arz、Text_EN.arc、Items.arc）を生成・検証・導入する。

計画書 6.8 ModBuilder、10 章（装備データ）、11 章（MOD の出力とインストール）に対応する。試作MOD 1〜3
（``prototype/mod01``〜``mod03``）の BUILD.md の手作業を置き換える。設計と使い方は ``docs/modbuilder.md``。

モジュール:

* :mod:`modbuilder.library`   装備ライブラリ（装備ID・表示名・生成情報・アセット・状態）
* :mod:`modbuilder.naming`    内部IDからレコードのパス・タグ名・アセットの置き場所を決める規則と、共通の定数
* :mod:`modbuilder.gamedata`  ゲームのインストール先を読み取り専用で参照する（.arz のレコード、.arc の一覧、テンプレート、公式タグ）
* :mod:`modbuilder.arz`       .arz の読み書き（試作MOD 1 で往復検証した自前コンパイラの移植）
* :mod:`modbuilder.arc`       .arc の読み取りと ArchiveTool による作成
* :mod:`modbuilder.records`   DBR レコードと表示名タグの生成
* :mod:`modbuilder.assets`    .msh / .tex の検査とテクスチャパスの付け替え
* :mod:`modbuilder.validate`  生成物の検証（参照先の実在、テクスチャの 2 の累乗、頂点数、ボーン名、往復）
* :mod:`modbuilder.builder`   統合ビルド
* :mod:`modbuilder.steam`     Steam ライブラリと Grim Dawn のインストール先の検出
* :mod:`modbuilder.install`   mods/ への導入・アンインストール・再導入・退避からの復元
* :mod:`modbuilder.cli`       コマンドライン（``python -I scripts/modbuilder.py``）

依存: lz4（BSD-3）、numpy と gdmesh（同梱。.msh の検査用）。ゲームのファイルは読むだけで変更しない。
"""
__version__ = "0.1.0"
