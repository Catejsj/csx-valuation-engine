"""One job that brings the data up to date: list the exchange's reports, download the new ones,
read them, then refresh prices and the exchange rate.

Progress goes to data/update.json so the app can show it. Safe to stop and run again: every step
skips what is already done. Runs at low priority so the computer stays usable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from src.core import paths, store

STATUS = "update.json"
EVERY = timedelta(days=7)          # start updates itself when the last one is older than this
HEARTBEAT = timedelta(minutes=10)  # a "running" job silent for longer than this is treated as stopped


def status_path() -> Path:
    return paths.data_dir() / STATUS


def read_status() -> dict:
    try:
        return json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(st: dict) -> None:
    st["heartbeat"] = datetime.now().isoformat(timespec="seconds")
    p = status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    for _ in range(5):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:   # Windows: the app is reading it this instant
            time.sleep(0.1)


def running(st: dict | None = None) -> bool:
    st = read_status() if st is None else st
    if st.get("state") != "running":
        return False
    try:
        return datetime.now() - datetime.fromisoformat(st["heartbeat"]) < HEARTBEAT
    except (KeyError, ValueError):
        return False


def due() -> bool:
    """True when no update has finished in the last week and none is running."""
    st = read_status()
    if running(st):
        return False
    try:
        return datetime.now() - datetime.fromisoformat(st["finished"]) > EVERY
    except (KeyError, ValueError):
        return True


def start_in_background(everything: bool = False) -> None:
    """Run `update` as a separate low-priority process; its output goes to data/update.log."""
    log = open(paths.data_dir() / "update.log", "a", encoding="utf-8")
    cmd = [sys.executable, "-m", "src.main", "update"] + (["--all"] if everything else [])
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONUTF8="1")   # report titles can hold any character; Windows logs need UTF-8
    if os.name == "nt":
        flags = 0x00004000 | 0x08000000      # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
        subprocess.Popen(cmd, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    else:
        subprocess.Popen(cmd, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def _lower_priority() -> None:
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass   # Windows: the process was started below normal priority instead


def reachable(timeout: float = 15) -> bool:
    """One quick look at the exchange's website, so an offline start says so in seconds, not minutes."""
    import urllib.error
    import urllib.request
    from src.collect import csx
    try:
        req = urllib.request.Request(csx.SITE, headers={"User-Agent": csx.USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True          # it answered, even if with an error page
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def run(everything: bool = False, log=print) -> int:
    conn = store.connect(paths.db_path())
    try:
        return _run(conn, everything, log)
    finally:
        conn.close()   # Windows keeps an open database file locked


def _run(conn, everything: bool, log) -> int:
    """The update itself. everything=False: annual reports only (about 700 MB the first time);
    True adds half-year and quarterly reports (about 2.5 GB in all)."""
    from src.collect import csx, market
    from src.collect.ingest import read_all
    _lower_priority()
    kinds = ("annual", "semiannual", "quarterly") if everything else ("annual",)
    store.init_db(conn)
    st = {"state": "running", "phase": "Looking for new reports on the exchange", "done": 0, "total": 0,
          "started": datetime.now().isoformat(timespec="seconds"), "new_reports": 0, "read": 0}
    _write(st)
    last = [0.0]

    def tick(force=False):
        if force or time.monotonic() - last[0] > 2:
            last[0] = time.monotonic()
            _write(st)

    def on_collect(line: str):
        log(line)
        if line.lstrip().startswith("+ "):
            st["new_reports"] += 1
            st["phase"] = f"Downloading new reports ({st['new_reports']} so far)"
        tick()

    try:
        if not reachable():
            raise csx.SourceUnavailable(f"{csx.SITE} did not answer")
        csx.sync(conn, paths.data_dir() / "pdfs", kinds=set(kinds), log=on_collect)
        marks = ",".join("?" * len(kinds))
        st["total"] = conn.execute(
            f"""SELECT COUNT(*) FROM documents WHERE sha256<>'' AND filing_id IS NULL AND kind IN ({marks})
                AND (read_status = '' OR read_status IS NULL)""", kinds).fetchone()[0]
        st["phase"] = "Reading the reports"
        tick(True)

        def on_read(line: str):
            log(line)
            st["done"] += 1
            st["read"] += 1
            tick()
        read_all(conn, kinds=kinds, log=on_read)
        st["phase"] = "Getting share prices and the exchange rate"
        tick(True)
        market.sync_fx(conn)
        market.sync_prices(conn)
    except csx.SourceUnavailable as e:
        st.update(state="offline", phase="The exchange's website did not answer. Showing the data already here; "
                                         "it tries again next time you start.", error=str(e))
        st["finished_try"] = datetime.now().isoformat(timespec="seconds")
        _write(st)
        log(st["phase"])
        return 3
    except Exception as e:  # noqa: BLE001 - say what went wrong, keep the app working
        st.update(state="failed", phase=f"The update stopped: {e}", error=str(e))
        _write(st)
        raise
    st.update(state="done", phase="Up to date", finished=datetime.now().isoformat(timespec="seconds"))
    _write(st)
    log(f"up to date: {st['new_reports']} new report(s), {st['read']} read")
    return 0
