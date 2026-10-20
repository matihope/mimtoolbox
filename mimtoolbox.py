#!/usr/bin/env python3
"""Entry point of mimtoolbox; see `mimtoolbox.py --help`. The code lives in the `mimtoolbox` package."""

import sys

from mimtoolbox.cli import main

if __name__ == "__main__":
    sys.exit(main())
