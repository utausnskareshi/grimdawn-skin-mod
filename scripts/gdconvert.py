# -*- coding: utf-8 -*-
"""gdconvert の起動用（``python -I scripts/gdconvert.py <コマンド> ...``）。

``-I``（隔離モード）では PYTHONPATH が無視されるので、ここで ``src`` を sys.path に入れてから
``gdconvert.cli.main`` を呼ぶ。コマンドは ``refs`` / ``convert`` / ``inspect``（``--help`` 参照）。
"""
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")))

from gdconvert.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
