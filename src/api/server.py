"""The read-only public API (JSON over HTTP, GET only).

    python -m src.main api                 serve on http://127.0.0.1:8780/v1
    python -m src.main export-api OUTDIR   write the same answers as static JSON files

Runs on this computer by default. Hosting it for others is a later step; the static
export is the cheapest way (any free static host can serve the folder).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from src.core import paths, store

from . import data

_local = threading.local()


def _conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = store.connect(paths.db_path())
        store.init_db(conn)
        _local.conn = conn
    return conn


def route(path: str, query: dict[str, str]) -> object:
    """Map a request path to data. Raises data.NotFound or ValueError."""
    conn = _conn()
    parts = [unquote(p) for p in path.strip("/").split("/") if p]
    if parts in ([], ["v1"]):
        return data.index()
    if parts[0] != "v1":
        raise data.NotFound(path)
    parts = parts[1:]
    if parts == ["companies"]:
        return data.companies(conn)
    if parts == ["documents"]:
        year = query.get("year")
        return data.documents(conn, symbol=query.get("symbol"), kind=query.get("kind"),
                              year=int(year) if year else None)
    if parts == ["fx"]:
        return data.fx(conn, query.get("from"))
    if parts == ["line-items"]:
        return data.line_items(conn)
    if parts == ["standard-items"]:
        from src.export.standard import dictionary
        return dictionary()
    if parts == ["schema", "statements.json"]:
        from src.export.standard import json_schema
        return json_schema()
    if len(parts) >= 2 and parts[0] == "companies":
        cid = parts[1].upper()
        if len(parts) == 2:
            return data.company(conn, cid)
        if parts[2:] == ["facts"]:
            return data.facts(conn, cid, statement=query.get("statement"),
                              period_type=query.get("period_type"), line_item=query.get("line_item"),
                              all_versions=query.get("all_versions") in ("1", "true"))
        if len(parts) == 4 and parts[2] == "series":
            return data.series(conn, cid, parts[3])
        if parts[2:] == ["prices"]:
            return data.prices(conn, cid, query.get("from"))
        if parts[2:] == ["report"]:
            return data.report(conn, cid)
        if parts[2:] == ["valuation"]:
            data.company(conn, cid)
            from src.value.engine import value
            return value(conn, cid, official_only=True)
        if parts[2:] in (["statements"], ["statements.json"]):
            data.company(conn, cid)
            from src.export.standard import to_json
            return to_json(conn, cid, official_only=True)
    raise data.NotFound(path)


class Handler(BaseHTTPRequestHandler):
    server_version = "csx-api"

    def log_message(self, format, *args):  # noqa: A002 - quiet terminal
        pass

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        c = re.fullmatch(r"/v1/companies/([A-Za-z0-9_.-]+)/statements\.csv", url.path)
        if c:
            try:
                data.company(_conn(), c.group(1).upper())
            except data.NotFound:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            from src.export.standard import to_csv
            body = to_csv(_conn(), c.group(1).upper(), official_only=True).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        m = re.fullmatch(r"/v1/companies/([A-Za-z0-9_.-]+)/workbook\.xlsx", url.path)
        if m:
            try:
                data.company(_conn(), m.group(1).upper())
            except data.NotFound:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            from src.export.workbook import build_workbook
            body = build_workbook(_conn(), m.group(1).upper(), official_only=True).to_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{m.group(1).upper()}.xlsx"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            self._send(HTTPStatus.OK, route(url.path, query))
        except data.NotFound as e:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found", "what": str(e.args[0]) if e.args else ""})
        except ValueError as e:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(e)})

    def do_HEAD(self):
        self.do_GET()

    def _no(self):
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read-only API: GET only"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _no


def serve(port: int = 8780, host: str = "127.0.0.1") -> None:
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"API on http://{host}:{port}/v1  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def export(out_dir: Path) -> int:
    """Write every endpoint (without query filters) as static JSON. Returns the file count."""
    conn = _conn()
    written = 0

    def put(rel: str, payload: object) -> None:
        nonlocal written
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        written += 1

    put("v1/index.json", data.index())
    comps = data.companies(conn)
    put("v1/companies.json", comps)
    put("v1/documents.json", data.documents(conn))
    put("v1/line-items.json", data.line_items(conn))
    put("v1/fx.json", data.fx(conn))
    from src.export import standard
    put("v1/standard-items.json", standard.dictionary())
    put("v1/schema/statements.json", standard.json_schema())
    for c in comps:
        cid = c["id"]
        put(f"v1/companies/{cid}.json", data.company(conn, cid))
        put(f"v1/companies/{cid}/prices.json", data.prices(conn, cid))
        f = data.facts(conn, cid)
        if not f:
            continue
        put(f"v1/companies/{cid}/facts.json", f)
        put(f"v1/companies/{cid}/report.json", data.report(conn, cid))
        from src.export.workbook import build_workbook
        build_workbook(conn, cid, official_only=True).save(out_dir / f"v1/companies/{cid}/workbook.xlsx")
        written += 1
        put(f"v1/companies/{cid}/statements.json", standard.to_json(conn, cid, official_only=True))
        (out_dir / f"v1/companies/{cid}/statements.csv").write_text(standard.to_csv(conn, cid, official_only=True),
                                                                    encoding="utf-8")
        written += 1
        for item in sorted({x["line_item"] for x in f if x["period_type"] == "FY"}):
            put(f"v1/companies/{cid}/series/{item}.json", data.series(conn, cid, item))
    return written
