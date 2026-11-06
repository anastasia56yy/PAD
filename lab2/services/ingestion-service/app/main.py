"""ingestion-service: the lab1 grabber as a service — collects documents from utm.md / fcim.utm.md,
cleans them and exports the processed corpus.

Owns: the grabber manifest (SQLite) and the raw/processed documents (volume /data).
Publishes: documents.updated after a run that changed something (search-service re-indexes).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from fastapi import Depends
from fastapi.responses import FileResponse

from utm_common import events
from utm_common.logging import log_event
from utm_common.rag import rag_config
from utm_common.security import Identity, current_identity, require_admin
from utm_common.web import ServiceError, create_app

log = logging.getLogger("ingestion")
cfg = rag_config()
state = {"running": False, "last_run": None}


def check_data() -> None:
    from src.config import resolve  # noqa: PLC0415

    if not (resolve(cfg["paths"]["processed_dir"]) / "documents.jsonl").exists() and not state["running"]:
        raise RuntimeError("no processed documents yet: run POST /ingest/run")


app = create_app("ingestion-service", {"documents": check_data},
                 description="Document collection from the UTM and FCIM websites (WordPress API + PDF).")


def run_ingestion() -> None:
    from src.grabber.run import Grabber  # noqa: PLC0415
    from src.preprocessing.process import process_documents  # noqa: PLC0415

    started = time.perf_counter()
    try:
        stats = Grabber(cfg).run()
        path = process_documents(cfg)
        documents = sum(1 for _ in open(path, encoding="utf-8"))
        state["last_run"] = {**stats, "documents": documents, "seconds": round(time.perf_counter() - started, 1),
                             "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        log_event(log, "Ingestion finished", **state["last_run"])
        if stats["new"] or stats["updated"] or stats["deleted"]:
            events.publish("documents.updated", {"documents": documents, **stats})
    except Exception as exc:  # noqa: BLE001
        log.exception("Ingestion failed")
        state["last_run"] = {"error": str(exc)[:300], "finished_at": datetime.now(timezone.utc).isoformat()}
    finally:
        state["running"] = False


@app.post("/ingest/run", status_code=202, tags=["ingestion"], summary="Start a collection run (admin)")
def start_run(identity: Identity = Depends(current_identity)) -> dict:
    require_admin(identity)
    if state["running"]:
        raise ServiceError(409, "already_running", "A collection run is already in progress")
    state["running"] = True
    threading.Thread(target=run_ingestion, daemon=True).start()
    log_event(log, "Ingestion started", by_user=identity.user_id)
    return {"status": "started"}


@app.get("/ingest/status", tags=["ingestion"])
def status() -> dict:
    return {"running": state["running"], "last_run": state["last_run"]}


@app.get("/documents/export", tags=["ingestion"], summary="Processed corpus as JSON Lines (for search-service)")
def export() -> FileResponse:
    from src.config import resolve  # noqa: PLC0415

    path = resolve(cfg["paths"]["processed_dir"]) / "documents.jsonl"
    if not path.exists():
        raise ServiceError(404, "not_found", "No processed documents yet")
    return FileResponse(path, media_type="application/x-ndjson", filename="documents.jsonl")


@app.get("/documents/stats", tags=["ingestion"])
def documents_stats() -> dict:
    from src.config import resolve  # noqa: PLC0415
    from src.grabber.state import Manifest  # noqa: PLC0415

    manifest = Manifest(resolve(cfg["paths"]["data_dir"]) / "manifest.sqlite")
    rows = manifest.conn.execute(
        "SELECT site, doc_type, status, COUNT(*) FROM documents GROUP BY 1, 2, 3").fetchall()
    return {"by_status": [{"site": r[0], "type": r[1], "status": r[2], "count": r[3]} for r in rows]}
