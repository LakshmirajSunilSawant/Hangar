"""A team standup board — what a real internal tool on Hangar looks like.

The other examples are instruments: `whoami` proves identity headers arrive,
`sqlite-notes` proves storage survives a restart. This one is meant to look
like software a team would actually open every morning, so that the platform's
features show up as behaviour rather than as JSON.

Three things are worth reading the code for, because each is a thing this app
does *not* have to implement:

* **No login.** There is no password field, no session, no user table. Visitors
  are already authenticated when they arrive, and their email is in a header.
* **No permission system.** Whether someone may post is decided by the role
  Hangar attaches, and the app just reads it. Adding a person to the team is a
  sharing action in the dashboard, not a code change here.
* **No asset pipeline.** The CSS is inline and nothing is fetched from a CDN,
  so this works unchanged with egress denied — which is how Hangar runs apps
  by default.

The headers are trustworthy *only* because this app cannot be reached except
through the proxy that sets them. On an app with a published port they would be
worth nothing, since anyone could send them by hand.
"""

from __future__ import annotations

import html
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

# Hangar injects DATABASE_URL when the app is given a database. The fallback
# keeps this runnable outside Hangar, where /data does not exist.
DB_PATH = os.environ.get("DATABASE_URL", "sqlite:////tmp/standup.db").split("///", 1)[-1]

# Roles allowed to post. Everyone who can reach the app can read it — that
# decision was already made when someone was granted access at all.
CAN_POST = {"owner", "editor"}


def connect():
    return closing(sqlite3.connect(DB_PATH))


def create_schema() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY,
                author TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # The volume is empty the first time an app is deployed, so the schema has
    # to be created on startup rather than assumed.
    create_schema()
    yield


app = FastAPI(title="Standup", lifespan=lifespan)


class Visitor:
    """Who Hangar says this is. Absent when the app is reached directly."""

    def __init__(self, request: Request) -> None:
        self.email = request.headers.get("x-hangar-user") or ""
        self.role = request.headers.get("x-hangar-role") or ""

    @property
    def known(self) -> bool:
        return bool(self.email)

    @property
    def may_post(self) -> bool:
        return self.known and self.role in CAN_POST

    @property
    def name(self) -> str:
        return self.email.split("@")[0] if self.email else "someone"


@app.get("/", response_class=HTMLResponse)
def board(request: Request) -> str:
    visitor = Visitor(request)
    with connect() as db:
        rows = db.execute(
            "SELECT author, body, created_at FROM updates ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return page(visitor, rows)


@app.post("/posts")
def add(request: Request, body: str = Form(...)):
    visitor = Visitor(request)
    # Checked here, not just hidden in the template. A form that is merely
    # absent from the page is not an access control.
    if not visitor.may_post:
        return RedirectResponse("/", status_code=303)

    text = body.strip()
    if text:
        with connect() as db:
            db.execute(
                "INSERT INTO updates (author, body, created_at) VALUES (?, ?, ?)",
                (visitor.email, text[:2000], datetime.now(timezone.utc).isoformat()),
            )
            db.commit()
    # Redirect rather than render, so a refresh does not post twice.
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def when(iso: str) -> str:
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - moment
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 60 * 24:
        return f"{minutes // 60}h ago"
    return moment.strftime("%d %b")


def initials(email: str) -> str:
    name = email.split("@")[0]
    parts = [p for p in name.replace(".", " ").replace("_", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def page(visitor: Visitor, rows: list[tuple[str, str, str]]) -> str:
    # Everything user-supplied goes through html.escape. This is a platform
    # that runs other people's code; the examples should not be the sloppy part.
    if not visitor.known:
        banner = (
            '<div class="notice warn"><strong>No identity header.</strong> '
            "This app is meant to run behind Hangar, which authenticates "
            "visitors and says who they are. Reached directly, it has no way "
            "to know — so posting is disabled.</div>"
        )
    elif visitor.may_post:
        banner = ""
    else:
        banner = (
            '<div class="notice"><strong>Read-only.</strong> You have viewer '
            "access to this tool, so you can see the team's updates but not "
            "add your own. Ask an owner to make you an editor.</div>"
        )

    composer = (
        f"""
        <form class="composer" method="post" action="/posts">
          <textarea name="body" rows="3" maxlength="2000"
                    placeholder="What are you working on today, {html.escape(visitor.name)}?"
                    required></textarea>
          <button type="submit">Post update</button>
        </form>
        """
        if visitor.may_post
        else ""
    )

    if rows:
        items = "\n".join(
            f"""
            <li class="update">
              <div class="avatar">{html.escape(initials(author))}</div>
              <div class="content">
                <div class="meta">
                  <span class="author">{html.escape(author)}</span>
                  <span class="dot">·</span>
                  <span class="time">{html.escape(when(created_at))}</span>
                </div>
                <p class="body">{html.escape(body)}</p>
              </div>
            </li>
            """
            for author, body, created_at in rows
        )
        feed = f'<ul class="feed">{items}</ul>'
    else:
        feed = (
            '<div class="empty"><p>No updates yet.</p>'
            "<p class=\"muted\">Whatever gets posted here is stored in this "
            "app's own database, which survives restarts and redeploys.</p></div>"
        )

    who = (
        f'<span class="chip">{html.escape(visitor.email)}'
        f'<span class="role">{html.escape(visitor.role)}</span></span>'
        if visitor.known
        else '<span class="chip muted">not signed in</span>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Standup</title>
<style>{STYLE}</style>
</head>
<body>
  <header>
    <div class="wrap bar">
      <div>
        <h1>Standup</h1>
        <p class="sub">What the team is working on</p>
      </div>
      {who}
    </div>
  </header>
  <main class="wrap">
    {banner}
    {composer}
    {feed}
    <footer>
      Deployed by Hangar. This app has no login screen, no user table and no
      permission code &mdash; identity and access are the platform's job.
    </footer>
  </main>
</body>
</html>"""


STYLE = """
:root {
  --bg: #f6f7f9; --card: #fff; --line: #e3e6ea; --text: #14181d;
  --dim: #667085; --accent: #2f6feb; --warn-bg: #fff8e6; --warn-line: #f0d48a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14181d; --card: #1b2027; --line: #2b323b; --text: #e8eaed;
    --dim: #9aa4b2; --accent: #6ea8fe; --warn-bg: #2c2717; --warn-line: #5c4f22;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 680px; margin: 0 auto; padding: 0 20px; }
header { background: var(--card); border-bottom: 1px solid var(--line); }
.bar { display: flex; align-items: center; gap: 16px; padding: 18px 20px; }
.bar > div:first-child { flex: 1; }
h1 { margin: 0; font-size: 19px; letter-spacing: -0.01em; }
.sub { margin: 2px 0 0; color: var(--dim); font-size: 13px; }
main { padding: 24px 20px 48px; }
.chip {
  display: inline-flex; align-items: center; gap: 8px; font-size: 13px;
  background: var(--bg); border: 1px solid var(--line);
  border-radius: 999px; padding: 5px 12px; white-space: nowrap;
}
.chip .role {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--accent); font-weight: 600;
}
.muted { color: var(--dim); }
.notice {
  background: var(--card); border: 1px solid var(--line);
  border-left: 3px solid var(--accent);
  border-radius: 8px; padding: 12px 14px; margin-bottom: 20px; font-size: 14px;
}
.notice.warn { background: var(--warn-bg); border-color: var(--warn-line); border-left-color: #d99e00; }
.composer {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 14px; margin-bottom: 24px;
}
textarea {
  width: 100%; border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 12px; font: inherit; resize: vertical;
  background: var(--bg); color: var(--text);
}
textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
button {
  margin-top: 10px; background: var(--accent); color: #fff; border: 0;
  border-radius: 8px; padding: 9px 16px; font: inherit; font-weight: 600;
  cursor: pointer;
}
button:hover { filter: brightness(1.08); }
.feed { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.update {
  display: flex; gap: 12px; background: var(--card);
  border: 1px solid var(--line); border-radius: 10px; padding: 14px;
}
.avatar {
  flex: none; width: 34px; height: 34px; border-radius: 50%;
  background: var(--accent); color: #fff; font-size: 12px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.content { min-width: 0; flex: 1; }
.meta { font-size: 13px; color: var(--dim); }
.author { color: var(--text); font-weight: 600; }
.dot { margin: 0 6px; }
.body { margin: 4px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.empty {
  background: var(--card); border: 1px dashed var(--line);
  border-radius: 10px; padding: 28px; text-align: center;
}
.empty p { margin: 0 0 6px; }
footer {
  margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--line);
  color: var(--dim); font-size: 13px;
}
"""
