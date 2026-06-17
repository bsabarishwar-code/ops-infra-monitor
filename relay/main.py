"""OPS-Infra Relay — FastAPI server bridging agent <-> ops_infra_v3."""
import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Header, HTTPException, Request

RELAY_SECRET = os.environ.get("RELAY_SECRET", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def _auth(secret):
    if RELAY_SECRET and secret != RELAY_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(app):
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS triggers (
                app_id       TEXT PRIMARY KEY,
                triggered_at TIMESTAMPTZ NOT NULL,
                acked_at     TIMESTAMPTZ
            );
            CREATE TABLE IF NOT EXISTS reports (
                app_id       TEXT PRIMARY KEY,
                report_json  JSONB        NOT NULL,
                reported_at  TIMESTAMPTZ  NOT NULL
            );
        """)
        c.commit()
    finally:
        c.close()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def root():
    return {"status": "OPS-Infra Relay online"}


@app.post("/trigger/{app_id}")
def trigger(app_id: str, x_secret: str = Header(None)):
    _auth(x_secret)
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO triggers (app_id, triggered_at, acked_at)
               VALUES (%s, %s, NULL)
               ON CONFLICT (app_id) DO UPDATE
                 SET triggered_at = EXCLUDED.triggered_at,
                     acked_at     = NULL""",
            (app_id, datetime.now(timezone.utc)),
        )
        c.commit()
    finally:
        c.close()
    return {"ok": True}


@app.get("/poll/{app_id}")
def poll(app_id: str, x_secret: str = Header(None)):
    _auth(x_secret)
    c = _conn()
    try:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT triggered_at, acked_at FROM triggers WHERE app_id = %s",
            (app_id,),
        )
        row = cur.fetchone()
    finally:
        c.close()
    if not row:
        return {"triggered": False}
    triggered = row["acked_at"] is None or row["triggered_at"] > row["acked_at"]
    return {"triggered": triggered}


@app.post("/ack/{app_id}")
def ack(app_id: str, x_secret: str = Header(None)):
    _auth(x_secret)
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute(
            "UPDATE triggers SET acked_at = %s WHERE app_id = %s",
            (datetime.now(timezone.utc), app_id),
        )
        c.commit()
    finally:
        c.close()
    return {"ok": True}


@app.post("/report/{app_id}")
async def post_report(app_id: str, request: Request, x_secret: str = Header(None)):
    _auth(x_secret)
    body = await request.body()
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO reports (app_id, report_json, reported_at)
               VALUES (%s, %s::jsonb, %s)
               ON CONFLICT (app_id) DO UPDATE
                 SET report_json = EXCLUDED.report_json,
                     reported_at = EXCLUDED.reported_at""",
            (app_id, body.decode(), datetime.now(timezone.utc)),
        )
        c.commit()
    finally:
        c.close()
    return {"ok": True}


@app.get("/report/{app_id}")
def get_report(app_id: str, x_secret: str = Header(None)):
    _auth(x_secret)
    c = _conn()
    try:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT report_json, reported_at FROM reports WHERE app_id = %s",
            (app_id,),
        )
        row = cur.fetchone()
    finally:
        c.close()
    if not row:
        return {"report": None, "reported_at": None}
    return {
        "report":      row["report_json"],
        "reported_at": row["reported_at"].isoformat(),
    }
