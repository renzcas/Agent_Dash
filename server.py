import asyncio
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict

import torch
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from transformers import AutoModel, AutoTokenizer

from modules.complex_attention import (
    build_complex_attention,
    collapse_attention,
    phase_entropy,
)
from modules.trace_utils import stream_trace
from pulse import emit_pulse
from routing.unified_bridge import UnifiedEngineBridge

CONFIG_PATH = Path("config/pillars.json")
DEFAULT_CONFIG: Dict[str, Any] = {
    "unified_engine": {
        "mode": "http_poll",
        "base_url": "http://localhost:8001",
        "state_endpoint": "/api/unified/state",
        "step_endpoint": "/api/unified/step",
        "poll_interval_ms": 500,
    },
    "cavesharp_ws": "ws://localhost:8181",
}


def load_pillars_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return DEFAULT_CONFIG.copy()
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        merged = DEFAULT_CONFIG.copy()
        if isinstance(loaded, dict):
            merged.update(loaded)
            if isinstance(loaded.get("unified_engine"), dict):
                merged["unified_engine"] = {
                    **DEFAULT_CONFIG["unified_engine"],
                    **loaded["unified_engine"],
                }
        return merged
    except Exception:
        return DEFAULT_CONFIG.copy()


pillars_config = load_pillars_config()
unified_cfg = pillars_config.get("unified_engine", {})
unified_base_url = (
    unified_cfg.get("base_url")
    or os.environ.get("UNIFIED_ENGINE_URL")
    or "http://localhost:8001"
)
engine_bridge = UnifiedEngineBridge(
    base_url=unified_base_url,
    state_endpoint=unified_cfg.get("state_endpoint", "/api/unified/state"),
    step_endpoint=unified_cfg.get("step_endpoint", "/api/unified/step"),
)

tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
model = AutoModel.from_pretrained("bert-base-uncased")
input_ids = tokenizer("The agent is thinking...", return_tensors="pt")["input_ids"]

app = FastAPI(title="AgentDash Telemetry Server")


def get_metadata() -> Dict[str, Any]:
    return {
        "python_version": sys.version,
        "platform": platform.platform(),
        "executable": sys.executable,
        "conda_env": os.environ.get("CONDA_DEFAULT_ENV", "unknown"),
        "cwd": os.getcwd(),
        "timestamp": time.time(),
    }


async def event_stream(request: Request):
    session_id = f"session_{int(time.time())}"  # Unique per stream

    while True:
        if await request.is_disconnected():
            break

        # 1. Interpreter pulse
        interpreter_payload = json.dumps({
            "type": "interpreter_pulse",
            "data": get_metadata(),
            "stream_id": session_id,
        })
        yield f"data: {interpreter_payload}\n\n"
        await asyncio.sleep(2)

        # 2. Live unified engine snapshot pulse
        try:
            snapshot = await engine_bridge.fetch_latest_snapshot()
            snapshot_payload = json.dumps({
                "type": "unified_snapshot",
                "data": snapshot,
                "stream_id": session_id,
            })
            yield f"data: {snapshot_payload}\n\n"
        except Exception as exc:
            status_payload = json.dumps({
                "type": "unified_status",
                "status": "offline",
                "detail": str(exc),
                "stream_id": session_id,
            })
            yield f"data: {status_payload}\n\n"

        # 3. Agent pulses
        yield emit_pulse("Planner initialized", role="planner", stream_id=session_id)
        await asyncio.sleep(1)
        yield emit_pulse(
            "Critic evaluating plan...",
            role="critic",
            stream_id=session_id,
            critique="Too linear",
        )
        await asyncio.sleep(1)

        # 4. Token-level trace
        for token in ["The", "agent", "is", "thinking", "..."]:
            if await request.is_disconnected():
                return
            yield stream_trace(token, role="planner", stream_id=session_id)
            await asyncio.sleep(0.5)


@app.get("/api/pillars")
async def get_pillars():
    return {
        "pillars_config": pillars_config,
        "unified_engine_url": unified_base_url,
    }


@app.get("/api/unified/snapshot")
async def get_unified_snapshot():
    try:
        snapshot = await engine_bridge.fetch_latest_snapshot()
        return {"connected": True, "snapshot": snapshot}
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "connected": False,
                "unified_engine_url": unified_base_url,
                "error": str(exc),
            },
        )


@app.post("/api/unified/step")
async def step_unified_engine(payload: Dict[str, Any] = Body(default={})):
    dt = float(payload.get("dt", 0.1))
    state_override = payload.get("state")
    try:
        result = await engine_bridge.step_simulation(dt=dt, state_override=state_override)
        return {"stepped": True, "result": result}
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "stepped": False,
                "unified_engine_url": unified_base_url,
                "error": str(exc),
            },
        )


@app.get("/stream")
async def stream(request: Request):
    return StreamingResponse(event_stream(request), media_type="text/event-stream")


@app.get("/complex-attention")
async def stream_complex_attention(request: Request):
    outputs = model(input_ids, output_attentions=True)
    attention_tensor = torch.stack(outputs.attentions)

    A_complex = build_complex_attention(attention_tensor)
    collapsed = collapse_attention(A_complex)
    entropy = phase_entropy(A_complex)

    async def gen():
        if await request.is_disconnected():
            return
        body = json.dumps({
            "collapsed": collapsed.tolist(),
            "entropy": entropy.tolist(),
        })
        yield f"data: {body}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# Mount static files last so API and SSE routes always take precedence
if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")