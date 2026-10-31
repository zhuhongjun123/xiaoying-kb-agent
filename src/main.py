"""模块入口：python -m src.main --input examples/inputs/C2-conflict.input.json

与根目录 run.py 等价。保留这个形态是为了让评审用自己习惯的命令跑，
不必记住本项目的特殊入口。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 让 kb_agent 可导入

from kb_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
