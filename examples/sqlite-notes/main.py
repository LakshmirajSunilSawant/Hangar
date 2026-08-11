"""A tool that stores something — used to prove per-app storage persists.

Reads DATABASE_URL, which Hangar injects when the app is given a database.
Without one, the read-only root filesystem makes this impossible to run.
"""

import os
import sqlite3
from contextlib import closing

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Hangar sample — notes")

# sqlite:////data/app.db -> /data/app.db
DB_PATH = os.environ.get("DATABASE_URL", "sqlite:////tmp/app.db").split("///", 1)[-1]


def connect():
    return closing(sqlite3.connect(DB_PATH))


@app.on_event("startup")
def create_schema() -> None:
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)")
        db.commit()


class Note(BaseModel):
    body: str


@app.get("/")
def index() -> dict:
    with connect() as db:
        rows = db.execute("SELECT id, body FROM notes ORDER BY id").fetchall()
    return {
        "app": "sqlite-notes",
        "database": DB_PATH,
        "count": len(rows),
        "notes": [{"id": r[0], "body": r[1]} for r in rows],
    }


@app.post("/notes")
def add(note: Note) -> dict:
    with connect() as db:
        cursor = db.execute("INSERT INTO notes (body) VALUES (?)", (note.body,))
        db.commit()
        return {"id": cursor.lastrowid, "body": note.body}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
