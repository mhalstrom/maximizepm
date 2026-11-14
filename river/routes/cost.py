"""The cost view (#5857): GET /api/cost?period=today|7d|30d|all, and ?csv=sessions|items|projects for a table as a
CSV file. The work is in river/cost.py."""

from .. import core, cost
from ..core import RiverError


def report(h, path):
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    conn = core.connect()
    try:
        rep = cost.report(conn, q.get("period") or "today")
        part = q.get("csv")
        if part:
            if part not in ("sessions", "items", "projects"):
                return h._send(400, {"error": "csv is sessions, items or projects"})
            body = cost.csv_text(rep, part).encode()
            h.send_response(200)
            h.send_header("Content-Type", "text/csv; charset=utf-8")
            h.send_header("Content-Disposition", f'attachment; filename="maxpm-cost-{rep["period"]}-{part}.csv"')
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
            return None
        return h._send(200, rep)
    except RiverError as e:
        return h._send(400, {"error": str(e)})
    finally:
        conn.close()


GET = [
    (lambda path: path == "/api/cost", report),
]

OPS = {}
