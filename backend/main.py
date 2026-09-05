"""OWL FastAPI application entry point."""

from fastapi import FastAPI

app = FastAPI(title="OWL API")


@app.get("/health")
def health():
    return {"status": "ok"}
