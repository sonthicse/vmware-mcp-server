"""Entry point for running the server without installing the package.

    python main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from vmware_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
