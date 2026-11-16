"""HTTP surface for traceable-qa.

Skeleton only: the routes exist so the contract and the deployment can be exercised, and every
business route answers ``501`` until its stage is implemented.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="traceable-qa", version="0.1.0")


def _pending(stage: str) -> JSONResponse:
    return JSONResponse({"detail": f"not implemented yet ({stage})"}, status_code=501)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/upload")
def upload() -> JSONResponse:
    """Ingest a PDF or CSV into the runtime as page-level or row-level sources."""
    return _pending("P3 ingest")


@app.get("/api/documents")
def documents() -> JSONResponse:
    """List ingested documents with their lifecycle state."""
    return _pending("P3 ingest")


@app.post("/api/ask")
def ask() -> JSONResponse:
    """Answer a question with page-level or row-level citations, or refuse."""
    return _pending("P4 answer")
