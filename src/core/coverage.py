"""How many years of history a company has, and which calculations that allows.

Rules live in config/requirements.toml. A calculation without enough history is reported as
"needs N years, has M" — the engine never fills missing years with guesses.
"""
from __future__ import annotations

import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path

from src.core.paths import PROJECT_ROOT

RULES_FILE = PROJECT_ROOT / "config" / "requirements.toml"
CORE_ITEMS = {"BS": "total_assets", "IS": "net_income", "CF": "cfo"}


@dataclass(frozen=True)
class Readiness:
    calculation: str          # e.g. "value.dcf"
    ready: bool
    have: int
    need: int
    unit: str                 # "years" or "trading days"
    why: str


def load_rules(path: Path = RULES_FILE) -> dict[str, dict]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return {f"{group}.{name}": rule for group, items in raw.items() for name, rule in items.items()}


def full_years(conn: sqlite3.Connection, issuer_id: str, official_only: bool = False) -> list[int]:
    """Fiscal years where all three statements have their anchor line and it is not rejected."""
    sql = """SELECT f.statement, substr(f.period_end, 1, 4) AS y FROM facts f
             JOIN filings g ON g.id = f.filing_id
             WHERE f.issuer_id = ? AND f.period_type = 'FY' AND f.check_status != 'failed' AND g.included = 1
               AND ((f.statement='BS' AND f.line_item='total_assets')
                 OR (f.statement='IS' AND f.line_item='net_income')
                 OR (f.statement='CF' AND f.line_item='cfo'))"""
    if official_only:
        sql += " AND g.origin = 'official'"
    seen: dict[int, set[str]] = {}
    for row in conn.execute(sql, (issuer_id,)):
        seen.setdefault(int(row["y"]), set()).add(row["statement"])
    return sorted(y for y, stmts in seen.items() if stmts >= set(CORE_ITEMS))


def trading_days(conn: sqlite3.Connection, issuer_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM prices WHERE issuer_id = ?", (issuer_id,)).fetchone()[0]


def readiness(conn: sqlite3.Connection, issuer_id: str, rules: dict[str, dict] | None = None,
              official_only: bool = False) -> list[Readiness]:
    rules = rules or load_rules()
    years = len(full_years(conn, issuer_id, official_only))
    days = trading_days(conn, issuer_id)
    out = []
    for name, rule in rules.items():
        if "min_trading_days" in rule:
            need, have, unit = int(rule["min_trading_days"]), days, "trading days"
        else:
            need, have, unit = int(rule["min_years"]), years, "years"
        out.append(Readiness(name, have >= need, have, need, unit, rule.get("why", "")))
    return out
