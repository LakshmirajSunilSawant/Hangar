"""Smallest useful FastAPI app — used to exercise the Hangar deploy pipeline."""

import os
import platform

from fastapi import FastAPI

app = FastAPI(title="Hangar sample — FastAPI")


@app.get("/")
def index() -> dict:
    return {
        "app": "fastapi-hello",
        "message": "Deployed by Hangar.",
        "python": platform.python_version(),
        "hostname": platform.node(),
        "deployed_by": os.environ.get("HANGAR_APP_NAME", "unknown"),
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
