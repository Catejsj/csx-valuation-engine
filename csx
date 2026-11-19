#!/bin/sh
# Mac and Linux: ./csx   (Windows: csx.bat). Finds Python 3.11+ and runs the launcher.
here="$(cd "$(dirname "$0")" && pwd)"
for p in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$p" "$here/csx.py" "$@"
  fi
done
echo "csx: Python 3.11 or newer is needed. Get it from https://www.python.org/downloads/ and run ./csx again."
exit 1
