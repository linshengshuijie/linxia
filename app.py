"""兼容入口"""

from __future__ import annotations

import sys

from linxia.cli import main

__all__ = ["main"]


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-runtime"]:
        from linxia.diagnostics import main as check_runtime

        sys.exit(check_runtime())
    sys.exit(main())
