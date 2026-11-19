"""Make the public copy: only the files on the list below, then check them for private terms.

    python tools/release.py FOLDER      copy the public files into FOLDER (must be empty or new) and check them

Anything not on the list stays private: the worklog, the owner's planning notes, bug notes, the code
graph, retired code, data. A new file is private until it is added here, so nothing slips out by default.
The copy fails if the leak check or the personal-detail check finds anything.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PUBLIC_FILES = ["README.md", "LICENSE.md", "NOTICE.md", "pyproject.toml", "requirements.txt", ".gitignore",
                "csx", "csx.bat", "csx.py", "start.command",
                "tools/__init__.py", "tools/leakcheck.py", "tools/leakcheck_hashes.txt", "tools/release.py"]
PUBLIC_DIRS = [".github", "config", "docs", "src", "tests"]
NEVER = {"__pycache__"}

# Things that identify a person or a computer, whatever the denylist says.
PERSONAL = [
    (re.compile(r"/home/[a-z_][a-z0-9_-]*/|/Users/[A-Za-z0-9_-]+/|[A-Z]:\\\\Users\\\\", re.I), "a home folder path"),
    (re.compile(r"[A-Za-z0-9._%+-]+@(?!noreply\.)[A-Za-z0-9.-]+\.[a-z]{2,}"), "an email address"),
    (re.compile(r"\b(ghp|gho|github_pat|sk|xox[bp])[_-][A-Za-z0-9_]{16,}"), "something that looks like a token"),
]
ALLOWED_EMAILS = {"licensing@polyformproject.org"}


def public_files(root: Path = ROOT) -> list[Path]:
    out = [root / f for f in PUBLIC_FILES if (root / f).is_file()]
    for d in PUBLIC_DIRS:
        out += [p for p in sorted((root / d).rglob("*")) if p.is_file() and not NEVER & set(p.parts)]
    return out


def personal_hits(files: list[Path], root: Path) -> list[str]:
    hits = []
    for p in files:
        if p.suffix.lower() in {".png", ".jpg", ".woff2", ".pdf"}:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for n, line in enumerate(text.splitlines(), 1):
            for rx, what in PERSONAL:
                for m in rx.finditer(line):
                    if what == "an email address" and m.group(0).lower() in ALLOWED_EMAILS:
                        continue
                    hits.append(f"{p.relative_to(root).as_posix()}:{n}: {what}")
    return hits


def check(root: Path = ROOT) -> list[str]:
    """Everything wrong with the public file set: denylisted terms and personal details."""
    sys.path.insert(0, str(ROOT))
    from tools import leakcheck
    hashes = leakcheck.load_hashes(leakcheck.HASHES)
    files = public_files(root)
    report = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        if p.suffix.lower() not in leakcheck.TEXT_SUFFIXES:
            continue
        for _, h in leakcheck.scan_text(rel.replace("/", " "), hashes):
            report.append(f"{rel}: (file name) term {h[:10]}")
        for n, h in leakcheck.scan_text(p.read_text(encoding="utf-8", errors="ignore"), hashes):
            report.append(f"{rel}:{n}: term {h[:10]}")
    return report + personal_hits(files, root)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    problems = check()
    if problems:
        print("not copied: fix these first")
        print("\n".join("  " + p for p in problems))
        return 1
    dest = Path(argv[0]).resolve()
    if dest.exists() and any(dest.iterdir()):
        print(f"{dest} is not empty")
        return 1
    for p in public_files():
        target = dest / p.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    print(f"copied {len(public_files())} public files to {dest}; leak and personal-detail checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
