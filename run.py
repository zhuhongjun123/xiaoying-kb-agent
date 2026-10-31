"""一键入口：python run.py ask "..." / run-cases / dump

放根目录是为了让评审者不用配 PYTHONPATH 或装包。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from kb_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
