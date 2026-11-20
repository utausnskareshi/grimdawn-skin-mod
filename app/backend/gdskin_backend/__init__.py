"""gdskin_backend: GD Skin Studio（WPF アプリ）のローカル専用バックエンド。

3D 生成（TRELLIS などの生成 AI）・変換（gdconvert）・MOD 作成（modbuilder）をジョブとして実行し、進捗・取消・結果・エラーを
127.0.0.1 の HTTP + JSON で返す。設計は ``docs/app_architecture.md``。

モジュール:

* :mod:`gdskin_backend.paths`      リポジトリ・データフォルダーの場所と sys.path
* :mod:`gdskin_backend.settings`   バックエンドの設定（``<データ>\\app\\backend_settings.json``）
* :mod:`gdskin_backend.redact`     ログから個人情報・入力を除く
* :mod:`gdskin_backend.logs`       ログファイル
* :mod:`gdskin_backend.errors`     エラーコードと分類
* :mod:`gdskin_backend.procutil`   子プロセスの起動・出力の読み取り・木ごとの終了・親プロセスの監視
* :mod:`gdskin_backend.jobs`       ジョブ・順番待ちの列・イベント
* :mod:`gdskin_backend.providers`  3D 生成 AI（TRELLIS）。追加の取り決めは ``providers/base.py``
* :mod:`gdskin_backend.drafts`     生成結果の下書き
* :mod:`gdskin_backend.pipeline`   生成ジョブ（生成 → 変換）
* :mod:`gdskin_backend.worker_tasks` 作業プロセスで動く処理（変換・ビルド・基準メッシュ）
* :mod:`gdskin_backend.library_ops` 装備ライブラリの操作
* :mod:`gdskin_backend.modops`     MOD のビルド結果・導入・アンインストール・出力
* :mod:`gdskin_backend.envcheck`   環境チェック
* :mod:`gdskin_backend.consent`    CLIP の注意文への同意（D-011）
* :mod:`gdskin_backend.service`    上の部品をまとめたサービス
* :mod:`gdskin_backend.server`     HTTP の窓口
"""
__version__ = "1.0.0"
API_VERSION = 1
