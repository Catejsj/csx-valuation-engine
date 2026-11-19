"""Block a push that contains private terms, without the denylist revealing those terms.

The denylist (leakcheck_hashes.txt) holds salted SHA-256 hashes, not words. Every file is
lower-cased, split into word tokens, and each run of 1-4 consecutive tokens is hashed and
compared. A hit prints file:line and a hash prefix only, never the term.

    python tools/leakcheck.py                 # scan the project; exit 1 on any hit
    python tools/leakcheck.py --add "term"    # add a term (stored as a hash)
    python tools/leakcheck.py --hashes F --root D   # scan D against another hash file (tests)

Files listed in .leakcheck-ignore (one relative path per line) are skipped: only the
private dev copy uses it, for the owner's own notes.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

SALT = "wse2-leakcheck-v1:"
MAX_NGRAM = 4
TOKEN = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
ROOT = Path(__file__).resolve().parents[1]
HASHES = Path(__file__).with_name("leakcheck_hashes.txt")
SKIP_DIRS = {"graphify-out", ".git", ".venv", "venv", "__pycache__", "data", "build", "dist", ".pytest_cache"}
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".json", ".csv",
    ".html", ".css", ".js", ".ts", ".sh", ".spec", ".svg", ".bat", ".command", "",
}


def normalize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def term_hash(term: str) -> str:
    tokens = normalize(term)
    if not tokens:
        raise ValueError("term has no letters or digits")
    if len(tokens) > MAX_NGRAM:
        raise ValueError(f"term is longer than {MAX_NGRAM} words")
    return hashlib.sha256((SALT + " ".join(tokens)).encode("utf-8")).hexdigest()


def load_hashes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def scan_text(text: str, hashes: set[str]) -> list[tuple[int, str]]:
    """Return (line_number, hash) for every denylisted n-gram. Line numbers are 1-based."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        tokens = normalize(line)
        for n in range(1, MAX_NGRAM + 1):
            for i in range(len(tokens) - n + 1):
                h = hashlib.sha256((SALT + " ".join(tokens[i:i + n])).encode("utf-8")).hexdigest()
                if h in hashes:
                    hits.append((lineno, h))
    return hits


def iter_files(root: Path, ignored: set[str], hash_file: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root).as_posix()
        if rel in ignored or rel == ".leakcheck-ignore" or path.resolve() == hash_file.resolve():
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            yield path, rel


def scan_tree(root: Path, hash_file: Path) -> list[str]:
    hashes = load_hashes(hash_file)
    ignore_file = root / ".leakcheck-ignore"
    ignored = set()
    if ignore_file.exists():
        ignored = {ln.strip() for ln in ignore_file.read_text(encoding="utf-8").splitlines() if ln.strip()}
    report = []
    for path, rel in iter_files(root, ignored, hash_file):
        # File names count too: a private file name is a leak by itself.
        for _, h in scan_text(rel.replace("/", " "), hashes):
            report.append(f"{rel}: (file name) term {h[:10]}")
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, h in scan_text(text, hashes):
            report.append(f"{rel}:{lineno}: term {h[:10]}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Block pushes that contain private terms.")
    parser.add_argument("--add", metavar="TERM", help="add a private term to the denylist")
    parser.add_argument("--hashes", type=Path, default=HASHES)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    if args.add:
        h = term_hash(args.add)
        existing = load_hashes(args.hashes)
        if h in existing:
            print("already in the denylist")
            return 0
        with args.hashes.open("a", encoding="utf-8") as f:
            f.write(h + "\n")
        print(f"added {h[:10]}")
        return 0

    report = scan_tree(args.root, args.hashes)
    if report:
        print(f"leakcheck: {len(report)} private term(s) found. Remove them before pushing.")
        print("\n".join(report))
        return 1
    print(f"leakcheck: clean ({len(load_hashes(args.hashes))} terms checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
