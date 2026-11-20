# -*- coding: utf-8 -*-
"""ModBuilder の起動用（``python -I scripts/modbuilder.py <コマンド> ...``）。

``-I``（隔離モード）では PYTHONPATH が無視されるので、ここで ``src`` を sys.path に入れてから
``modbuilder.cli.main`` を呼ぶ。コマンドの一覧は ``--help``、設計と使い方は docs/modbuilder.md。
"""
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")))

from modbuilder.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
