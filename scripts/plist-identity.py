#!/usr/bin/env python3
"""Print SHA-256 and Label derived from one plist byte snapshot."""

from __future__ import annotations

import hashlib
import plistlib
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        data = Path(sys.argv[1]).read_bytes()
        label = plistlib.loads(data).get("Label")
    except (OSError, plistlib.InvalidFileException):
        return 1
    if not isinstance(label, str) or not label or "\n" in label or "\t" in label:
        return 1
    print(f"{hashlib.sha256(data).hexdigest()}\t{label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
