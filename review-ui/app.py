"""Local-only review UI backend."""
from __future__ import annotations

import sqlite3
import uuid
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import confirmation_snapshot_hash, connect, migrate

DATABASE = Path("data/gmail_cleanup.db")
app = FastAPI(title="Gmail Cleanup Review")


def database() -> sqlite3.Connection:
    connection = connect(DATABASE)
    migrate(connection)
    return connection


class Decision(BaseModel):
    status: str


class WindowExtension(BaseModel):
    days: int


class Confirmation(BaseModel):
    note: str = ""


@app.get("/api/groups")
def groups(category: str | None = None):
    connection = database()
    try:
        sql = "SELECT * FROM sender_groups"
        args: list[str] = []
        if category:
            sql += " WHERE category=?"
            args.append(category)
        sql += " ORDER BY delete_safety_score DESC, total_size_bytes DESC"
        return [dict(row) for row in connection.execute(sql, args)]
    finally:
        connection.close()


@app.get("/api/groups/{group_key}")
def group_detail(group_key: str):
    connection = database()
    try:
        group = connection.execute("SELECT * FROM sender_groups WHERE group_key=?", (group_key,)).fetchone()
        if not group:
            raise HTTPException(404, "Unknown sender group")
        messages = connection.execute(
            """SELECT m.* FROM messages m JOIN group_members gm ON lower(m.sender_email)=gm.sender_email
               WHERE gm.group_key=? ORDER BY m.date DESC LIMIT 20""", (group_key,)
        ).fetchall()
        rationales = connection.execute(
            """SELECT DISTINCT i.rationale FROM sender_identity i JOIN group_members gm ON i.sender_email=gm.sender_email
               WHERE gm.group_key=?""", (group_key,)
        ).fetchall()
        return {"group": dict(group), "messages": [dict(row) for row in messages], "rationales": [row[0] for row in rationales]}
    finally:
        connection.close()


@app.post("/api/groups/{group_key}/decision")
def decide_group(group_key: str, decision: Decision):
    if decision.status not in {"approved", "rejected", "skipped"}:
        raise HTTPException(422, "Invalid decision")
    connection = database()
    try:
        with connection:
            group = connection.execute("SELECT * FROM sender_groups WHERE group_key=?", (group_key,)).fetchone()
            if not group or group["approval_status"] != "pending":
                raise HTTPException(409, "Group is no longer pending")
            if group["category"] == "Business-critical" and decision.status == "approved":
                raise HTTPException(403, "Business-critical groups cannot be approved")
            if decision.status != "approved":
                connection.execute("UPDATE sender_groups SET approval_status=? WHERE group_key=?", (decision.status, group_key))
                return {"status": decision.status}
            rows = connection.execute(
                """SELECT m.message_id,m.size_bytes FROM messages m JOIN group_members gm ON lower(m.sender_email)=gm.sender_email
                   WHERE gm.group_key=?""", (group_key,)
            ).fetchall()
            batch_id = str(uuid.uuid4())
            count, total = len(rows), sum(row["size_bytes"] for row in rows)
            connection.execute("INSERT INTO batches (batch_id,group_key,status,approved_at,message_count,total_size_bytes) VALUES (?,?, 'approved',unixepoch(),?,?)", (batch_id, group_key, count, total))
            connection.executemany("INSERT INTO batch_messages (batch_id,message_id,status) VALUES (?,?,'pending')", [(batch_id, row["message_id"]) for row in rows])
            connection.execute("UPDATE sender_groups SET approval_status='approved' WHERE group_key=?", (group_key,))
            connection.execute("INSERT INTO audit_log (batch_id,event,message_count,timestamp,note) VALUES (?,'approved',?,unixepoch(),'')", (batch_id, count))
            return {"status": "approved", "batch_id": batch_id, "message_count": count, "total_size_bytes": total}
    finally:
        connection.close()


@app.get("/api/batches")
def batches():
    connection = database()
    try:
        values = []
        for row in connection.execute("SELECT * FROM batches ORDER BY approved_at DESC"):
            value = dict(row)
            if value["status"] == "restore_window" and value["restore_deadline"] < time.time():
                value["display_status"] = "delete_pending"
            else:
                value["display_status"] = value["status"]
            values.append(value)
        return values
    finally:
        connection.close()


@app.post("/api/batches/{batch_id}/extend-window")
def extend_window(batch_id: str, extension: WindowExtension):
    if extension.days <= 0:
        raise HTTPException(422, "Extension must be at least one day")
    connection = database()
    try:
        with connection:
            batch = connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if not batch or batch["status"] != "restore_window":
                raise HTTPException(409, "Only restore-window batches can be extended")
            seconds = extension.days * 86_400
            connection.execute("UPDATE batches SET restore_deadline=restore_deadline+?, window_extensions=window_extensions+1 WHERE batch_id=?", (seconds, batch_id))
            connection.execute("INSERT INTO audit_log (batch_id,event,message_count,timestamp,note) VALUES (?,'window_extended',0,unixepoch(),?)", (batch_id, f"extended +{extension.days}d"))
        return {"batch_id": batch_id, "extended_days": extension.days}
    finally:
        connection.close()


@app.post("/api/batches/{batch_id}/confirm-trash")
def confirm_trash(batch_id: str, confirmation: Confirmation):
    connection = database()
    try:
        with connection:
            batch = connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if not batch or batch["status"] != "restore_window" or batch["restore_deadline"] >= time.time():
                raise HTTPException(409, "Batch is not eligible for Trash confirmation")
            digest = confirmation_snapshot_hash(connection, batch_id)
            connection.execute("UPDATE batches SET permanent_delete_confirmed_at=unixepoch(), confirmation_snapshot_hash=? WHERE batch_id=?", (digest, batch_id))
            labeled_count = connection.execute("SELECT COUNT(*) FROM batch_messages WHERE batch_id=? AND status='labeled'", (batch_id,)).fetchone()[0]
            connection.execute("INSERT INTO audit_log (batch_id,event,message_count,timestamp,note) VALUES (?,'permanent_delete_confirmed',?,unixepoch(),?)", (batch_id, labeled_count, confirmation.note))
        return {"batch_id": batch_id, "confirmation_snapshot_hash": digest}
    finally:
        connection.close()


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
