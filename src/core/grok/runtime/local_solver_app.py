"""
托管本地 Solver 的占位 HTTP 服务。
"""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Grok Local Solver", version="0.1.0")


class TurnstileRequest(BaseModel):
    page_url: str | None = None
    sitekey: str | None = None
    action: str | None = None


@app.get("/health")
async def health():
    return {"ok": True, "service": "grok-local-solver", "mode": "placeholder"}


@app.post("/turnstile")
async def solve_turnstile(request: TurnstileRequest):
    return {
        "success": False,
        "error": "Managed local solver placeholder is running, but the upstream solver implementation is not installed.",
        "page_url": request.page_url,
        "sitekey": request.sitekey,
        "action": request.action,
    }


@app.get("/result/{request_id}")
async def solver_result(request_id: str):
    return {
        "success": False,
        "error": "Managed local solver placeholder does not persist results.",
        "request_id": request_id,
    }
