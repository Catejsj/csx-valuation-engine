"""Launcher: sets everything up the first time, then runs a command.

    csx                 set up (first time), open the app, update the data when due
    csx update          get new reports and prices now
    csx value ABC       valuation with every step
    csx export ABC      Excel workbook (add csv or json for the machine-friendly version)
    csx api             read-only JSON API
    csx test            run the tests
    csx help            every command

Works on Windows, macOS and Linux with Python 3.11 or newer. Uses only the standard library, so it
runs before anything is installed.
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQ = ROOT / "requirements.txt"
STAMP = VENV / ".csx-requirements"


def say(msg: str) -> None:
    print(f"csx: {msg}", flush=True)


def setup() -> None:
    want = hashlib.sha256(REQ.read_bytes()).hexdigest()
    if PY.exists() and STAMP.exists() and STAMP.read_text().strip() == want:
        return
    if not PY.exists():
        say("first run: setting up (about a minute, only once)")
        if subprocess.call([sys.executable, "-m", "venv", str(VENV)]) != 0 or not PY.exists():
            say("could not create the environment")
            if sys.platform.startswith("linux"):
                say("on Debian/Ubuntu, install it with:  sudo apt install python3-venv")
            sys.exit(1)
    say("installing the two packages it needs (pdfplumber, pypdfium2)")
    r = subprocess.call([str(PY), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(REQ)])
    if r != 0:
        say("installing failed: check the internet connection and run csx again")
        sys.exit(r)
    STAMP.write_text(want)
    say("set up")


def main() -> int:
    if sys.version_info < (3, 11):
        say(f"Python 3.11 or newer is needed (this is {sys.version.split()[0]}). "
            "Get it from https://www.python.org/downloads/ and run csx again.")
        return 1
    args = sys.argv[1:]
    setup()
    env = dict(os.environ, PYTHONUTF8="1",
               PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    if args[:1] == ["test"]:
        cmd = [str(PY), "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-t", str(ROOT)] + args[1:]
        return subprocess.call(cmd, cwd=ROOT, env=env)
    if args[:1] in (["help"], ["-h"], ["--help"]):
        args = ["--help"]
    try:
        return subprocess.call([str(PY), "-m", "src.main", *args], env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
