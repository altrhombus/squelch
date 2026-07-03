"""Audio stream and live-metadata WebSocket."""

import asyncio
import json

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from .. import context

router = APIRouter()


@router.get("/stream")
async def audio_stream(request: Request):
    """
    Chunked HTTP AAC-LC/ADTS stream.
    Content-Type: audio/aac — natively decoded on iOS/macOS, Chrome 89+, AirPlay.
    Each connected client gets its own asyncio.Queue; slow clients drop frames.
    """
    q = context.streams.new_client()

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=10.0)
                    yield chunk
                except asyncio.TimeoutError:
                    # Keep the connection alive even if SDR is stopped
                    pass
        finally:
            context.streams.remove_client(q)

    return StreamingResponse(
        generate(),
        media_type="audio/aac",
        headers={
            "Cache-Control":        "no-cache, no-store",
            "X-Accel-Buffering":    "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/stream/url")
async def stream_url():
    return {"stream_url": "/stream", "content_type": "audio/aac"}


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    context.meta.register_ws(ws)
    try:
        await ws.send_text(json.dumps(context.meta.to_dict()))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        context.meta.unregister_ws(ws)
