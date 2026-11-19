"""Command line entry point.

    python -m src.main init-db     create the database (safe to run twice)
    python -m src.main status      show where data lives and how many records exist
    python -m src.main extract PDF --issuer ID --name NAME --filing-id ID --url URL --published YYYY-MM-DD
                                   read the statements, run the checks, store proposed facts
    python -m src.main review      open the review screen (PDF page beside each value)
    python -m src.main upload PDF --issuer ID --name NAME   add your own filing (checked first)
    python -m src.main collect     index + download every report from the exchange website (polite, resumable)
    python -m src.main read        read statements from downloaded reports (annual by default)
    python -m src.main export ID   Excel workbook for one company (statements, ratios, prices, sources)
    python -m src.main api         read-only JSON API on http://127.0.0.1:8780/v1
    python -m src.main export-api DIR   the same API as static JSON files
    python -m src.main coverage ID show years of history and which calculations they allow
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date, datetime
from pathlib import Path

from src.core import paths, store


def cmd_init_db() -> int:
    conn = store.connect(paths.db_path())
    store.init_db(conn)
    print(f"database ready: {paths.db_path()}")
    return 0


def cmd_status() -> int:
    db = paths.db_path()
    print(f"data folder: {paths.data_dir()}")
    if not db.exists():
        print("no database yet; run: python -m src.main init-db")
        return 0
    conn = store.connect(db)
    store.init_db(conn)
    for table, n in store.counts(conn).items():
        print(f"  {table:<9} {n}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    from src.core.schema import Filing, Issuer
    from src.extract.load import build, save
    from src.extract.statements import extract_statements

    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"no such file: {pdf}")
        return 2
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    tables = extract_statements(pdf)
    if not tables:
        print("no statements found (scanned PDF or unknown layout); enter values manually")
        return 1
    report = build(tables, args.issuer, args.filing_id, args.kind)

    for t in tables:
        print(f"{t.statement.value}: pages {t.pages}, {len(t.rows)} rows, period end {t.period_end}")
    passed = sum(c.status == "pass" for c in report.checks)
    print(f"checks: {passed} pass, {len(report.failed)} fail, "
          f"{sum(c.status == 'cannot check' for c in report.checks)} cannot check")
    for c in report.checks:
        if c.status != "pass":
            print(f"  {c.status.upper():<12} {c.statement.value} {c.column}: {c.name} "
                  f"(left {c.left}, right {c.right}, diff {c.diff}) pages {list(c.pages)}")
    for p in report.problems:
        print(f"  needs review: {p}")
    for line in report.implied_fx:
        print(f"  info: {line}")
    if args.dry_run:
        print(f"dry run: {len(report.facts)} facts not stored")
        return 0 if not report.failed else 1

    conn = store.connect(paths.db_path())
    store.init_db(conn)
    issuer = Issuer(id=args.issuer, name=args.name, reporting_currency=tables[0].columns[0].currency)
    filing = Filing(id=args.filing_id, issuer_id=args.issuer, kind=args.kind, source_url=args.url,
                    pdf_sha256=sha, published_on=date.fromisoformat(args.published),
                    title=args.title or pdf.name, retrieved_at=datetime.now().replace(microsecond=0))
    n = save(conn, issuer, filing, report)
    print(f"stored {n} proposed facts for {args.issuer} / {args.filing_id} (sha256 {sha[:12]}...)")
    return 0 if not report.failed else 1


def cmd_upload(args: argparse.Namespace) -> int:
    from src.intake.upload import ingest
    r = ingest(Path(args.pdf), args.issuer, args.name, args.kind)
    if not r.accepted:
        print("refused:")
        for why in r.refused:
            print(f"  - {why}")
        return 1
    print(f"accepted: {r.pages} pages ({r.text_pages} with text), sha256 {r.sha256[:12]}...")
    if r.facts_stored:
        print(f"stored {r.facts_stored} proposed values as {r.filing_id} (your upload; not public)")
        print(f"checks: {r.checks_passed} pass, {r.checks_failed} fail")
    for w in r.warnings:
        print(f"  note: {w}")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    from src.core.coverage import full_years, readiness
    conn = store.connect(paths.db_path())
    store.init_db(conn)
    years = full_years(conn, args.issuer)
    print(f"{args.issuer}: {len(years)} full year(s) {years}")
    for r in readiness(conn, args.issuer):
        mark = "ready  " if r.ready else "not yet"
        print(f"  {mark} {r.calculation:<26} has {r.have}/{r.need} {r.unit}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from src.collect import csx
    conn = store.connect(paths.db_path())
    store.init_db(conn)
    try:
        stats = csx.sync(conn, paths.data_dir() / "pdfs",
                         boards=tuple(args.board), only=set(args.symbol) or None,
                         kinds=set(args.kind) or None, download=not args.index_only)
    except csx.SourceUnavailable as e:
        print(f"the exchange site did not answer; try again later or from another network ({e})")
        return 3
    print(" ".join(f"{k}={v}" for k, v in stats.items()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="csx", description="Cambodian listed-company valuation engine. "
                                     "Run with no command to set up and open the app.")
    sub = parser.add_subparsers(dest="command", metavar="command", title="commands (more: see docs/COMMANDS.md)")
    st = sub.add_parser("start", help="open the app (the default); updates the data in the background when due")
    st.add_argument("--port", type=int, default=8770)
    st.add_argument("--no-open", action="store_true", help="do not open a browser")
    st.add_argument("--no-update", action="store_true", help="do not fetch new reports this time")
    ud = sub.add_parser("update", help="download new reports from the exchange, read them, refresh prices")
    ud.add_argument("--all", action="store_true", help="also half-year and quarterly reports (about 2.5 GB in all)")
    sub.add_parser("init-db", help=argparse.SUPPRESS, description="create the database")
    sub.add_parser("status", help=argparse.SUPPRESS, description="show data location and record counts")
    ex = sub.add_parser("extract", help=argparse.SUPPRESS, description="read statements from a PDF and store proposed facts")
    ex.add_argument("pdf")
    ex.add_argument("--issuer", required=True, help="ticker, e.g. as listed on the exchange")
    ex.add_argument("--name", required=True, help="company name")
    ex.add_argument("--filing-id", required=True)
    ex.add_argument("--url", required=True, help="where the PDF was published")
    ex.add_argument("--published", required=True, help="publication date, YYYY-MM-DD")
    ex.add_argument("--kind", default="annual", choices=["annual", "semiannual", "quarterly", "other"])
    ex.add_argument("--title", default="")
    ex.add_argument("--dry-run", action="store_true", help="check only, store nothing")
    rv = sub.add_parser("review", help=argparse.SUPPRESS, description="open the review screen in the browser")
    rv.add_argument("--port", type=int, default=8770)
    rv.add_argument("--no-open", action="store_true", help="do not open a browser")
    up = sub.add_parser("upload", help="add your own filing PDF (checked before it is accepted)")
    up.add_argument("pdf")
    up.add_argument("--issuer", required=True)
    up.add_argument("--name", required=True)
    up.add_argument("--kind", default="annual", choices=["annual", "semiannual", "quarterly", "other"])
    cv = sub.add_parser("coverage", help=argparse.SUPPRESS, description="years of history and which calculations they allow")
    cv.add_argument("issuer")
    co = sub.add_parser("collect", help=argparse.SUPPRESS, description="index and download reports from the exchange website")
    co.add_argument("--board", nargs="*", default=["stock", "bond"], choices=["stock", "bond"])
    co.add_argument("--symbol", nargs="*", default=[], help="only these tickers")
    co.add_argument("--kind", nargs="*", default=[], choices=["annual", "semiannual", "quarterly", "other"])
    co.add_argument("--index-only", action="store_true", help="list reports, download nothing")
    mk = sub.add_parser("market", help=argparse.SUPPRESS, description="update daily share prices and the KHR/USD rate from the exchange")
    mk.add_argument("--full", action="store_true", help="re-download the whole history, not just new days")
    rd = sub.add_parser("read", help=argparse.SUPPRESS, description="read statements from downloaded reports into the data")
    rd.add_argument("--kind", nargs="*", default=["annual"], choices=["annual", "semiannual", "quarterly", "other"])
    rd.add_argument("--symbol", nargs="*", default=[])
    ap = sub.add_parser("api", help="serve the read-only JSON API on this computer")
    ap.add_argument("--port", type=int, default=8780)
    xl = sub.add_parser("export", help="write a company's data: Excel workbook, or standardised statements as JSON/CSV")
    xl.add_argument("issuer")
    xl.add_argument("fmt", nargs="?", choices=["xlsx", "json", "csv"], help="short for --format")
    xl.add_argument("--format", default="xlsx", choices=["xlsx", "json", "csv"],
                    help="xlsx = workbook for people; json / csv = standardised statements for programs")
    xl.add_argument("--out", default="", help="file name (default: ID-date.<format> in the current folder)")
    xl.add_argument("--public", action="store_true", help="official filings only (leave out your uploads)")
    va = sub.add_parser("value", help="value lens: what the equity may be worth, as a range, with every input sourced")
    va.add_argument("issuer")
    va.add_argument("--json", action="store_true", help="print the full result as JSON")
    ex2 = sub.add_parser("export-api", help=argparse.SUPPRESS, description="write the API as static JSON files into a folder")
    ex2.add_argument("out")
    sub._choices_actions = [a for a in sub._choices_actions if a.help != argparse.SUPPRESS]
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["start", *argv]          # "csx" and "csx --no-open" both mean start
    args = parser.parse_args(argv)
    if args.command == "start":
        from src import update
        from src.review.server import serve
        conn = store.connect(paths.db_path())
        store.init_db(conn)
        empty = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 0
        conn.close()
        if not args.no_update and update.due():
            update.start_in_background()
            print("getting new reports from the exchange in the background"
                  + (" (the first time takes a while: about 700 MB; companies appear as they are read)" if empty else "")
                  + "; progress shows in the app")
        serve(args.port, not args.no_open)
        return 0
    if args.command == "update":
        from src import update
        return update.run(everything=args.all)
    if args.command == "read":
        from src.collect.ingest import read_all
        conn = store.connect(paths.db_path())
        store.init_db(conn)
        stats = read_all(conn, kinds=tuple(args.kind), only=set(args.symbol) or None)
        print(" ".join(f"{k}={v}" for k, v in stats.items()) or "nothing new to read")
        return 0
    if args.command == "api":
        from src.api.server import serve as serve_api
        serve_api(args.port)
        return 0
    if args.command == "value":
        import json as _json
        from src.value.engine import value
        conn = store.connect(paths.db_path())
        store.init_db(conn)
        r = value(conn, args.issuer, official_only=False)
        if args.json:
            print(_json.dumps(r, indent=1, ensure_ascii=False))
            return 0
        print(f"{r['company']} {r['name']}: {r['sector'] or '-'} / model {r['model'] or '-'} / {r['status']}")
        for s_, v_ in r["steps"]:
            print(f"  {s_:<100} {v_:>16,}" if isinstance(v_, (int, float)) else f"  {s_}")
        if r["value"]:
            v_ = r["value"]
            print(f"  equity value (USD): bear {v_['bear']:,}  base {v_['base']:,}  bull {v_['bull']:,}")
        if r["market"]:
            print(f"  market cap {r['market']['market_cap_usd']:,} USD on {r['market']['as_of']}: {r['market']['market_cap_vs_range']}")
        if r["liquidity"]:
            l_ = r["liquidity"]
            print(f"  liquidity: {l_['band']} (discount {l_['discount'][0]:.0%}-{l_['discount'][1]:.0%}); "
                  f"after discount {l_['value_after_discount']['low']:,} - {l_['value_after_discount']['high']:,}")
        for n in r["missing"] + r["notes"]:
            print("  - " + n)
        print("  " + r["disclaimer"])
        return 0
    if args.command == "export":
        conn = store.connect(paths.db_path())
        store.init_db(conn)
        if args.fmt:
            args.format = args.fmt
        out = Path(args.out or f"{args.issuer}-{date.today().isoformat()}.{args.format}")
        if args.format == "xlsx":
            from src.export.workbook import build_workbook
            build_workbook(conn, args.issuer, official_only=args.public).save(out)
        else:
            import json as _json
            from src.export import standard
            text = (_json.dumps(standard.to_json(conn, args.issuer, official_only=args.public), ensure_ascii=False, indent=1)
                    if args.format == "json" else standard.to_csv(conn, args.issuer, official_only=args.public))
            out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
        return 0
    if args.command == "export-api":
        from src.api.server import export
        n = export(Path(args.out))
        print(f"wrote {n} files to {args.out}")
        return 0
    if args.command == "market":
        from src.collect import csx, market
        conn = store.connect(paths.db_path())
        store.init_db(conn)
        try:
            market.sync_fx(conn, full=args.full)
            market.sync_prices(conn, full=args.full)
        except csx.SourceUnavailable as e:
            print(f"the exchange site did not answer; try again later ({e})")
            return 3
        return 0
    if args.command == "collect":
        return cmd_collect(args)
    if args.command == "upload":
        return cmd_upload(args)
    if args.command == "coverage":
        return cmd_coverage(args)
    if args.command == "review":
        from src.review.server import serve
        serve(args.port, not args.no_open)
        return 0
    if args.command == "extract":
        return cmd_extract(args)
    return {"init-db": cmd_init_db, "status": cmd_status}[args.command]()


if __name__ == "__main__":
    sys.exit(main())
