\
from datetime import datetime
from typing import Dict, List
import re

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Keralee & Kayden Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROFANITY_PATTERNS = [
    re.compile(r"\bdamn\b", re.I),
    re.compile(r"\bhell\b", re.I),
    re.compile(r"\bcrap\b", re.I),
    re.compile(r"\bwtf\b", re.I),
    re.compile(r"\bidiot\b", re.I),
    re.compile(r"\bstupid\b", re.I),
    re.compile(r"\bshut up\b", re.I),
]

CHANNELS = ["studio-keralee", "studio-kayden", "studio-together"]
COMMENTS: Dict[str, List[dict]] = {channel: [] for channel in CHANNELS}
JOURNAL: List[dict] = []
CONNECTIONS: Dict[str, List[WebSocket]] = {channel: [] for channel in CHANNELS}


class LoginRequest(BaseModel):
    username: str
    password: str


class CommentCreate(BaseModel):
    author: str
    message: str


class JournalCreate(BaseModel):
    title: str
    date: str
    host: str
    category: str
    story: str


def contains_profanity(text: str) -> bool:
    return any(pattern.search(text) for pattern in PROFANITY_PATTERNS)


async def broadcast(channel: str, payload: dict) -> None:
    dead = []
    for ws in CONNECTIONS[channel]:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in CONNECTIONS[channel]:
            CONNECTIONS[channel].remove(ws)


@app.get("/api/health")
def health():
    return {"ok": True, "service": "fastapi", "channels": CHANNELS}


@app.post("/api/auth/login")
def login(body: LoginRequest):
    # Replace with real auth/JWT/session handling.
    if body.username and body.password:
        return {"ok": True, "role": "internal"}
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.get("/api/comments/{channel}")
def list_comments(channel: str):
    if channel not in COMMENTS:
        raise HTTPException(status_code=404, detail="Unknown channel")
    return COMMENTS[channel]


@app.post("/api/comments/{channel}")
async def create_comment(channel: str, body: CommentCreate):
    if channel not in COMMENTS:
        raise HTTPException(status_code=404, detail="Unknown channel")
    if contains_profanity(body.message):
        raise HTTPException(status_code=400, detail="Comment blocked by family-safe filter")
    item = {
        "author": body.author.strip(),
        "message": body.message.strip(),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    COMMENTS[channel].insert(0, item)
    await broadcast(channel, {"type": "comment_created", "channel": channel, "item": item})
    return {"ok": True, "item": item}


@app.delete("/api/comments/{channel}/{index}")
async def delete_comment(channel: str, index: int):
    if channel not in COMMENTS:
        raise HTTPException(status_code=404, detail="Unknown channel")
    if index < 0 or index >= len(COMMENTS[channel]):
        raise HTTPException(status_code=404, detail="Comment not found")
    item = COMMENTS[channel].pop(index)
    await broadcast(channel, {"type": "comment_deleted", "channel": channel, "index": index})
    return {"ok": True, "deleted": item}


@app.get("/api/journal")
def list_journal():
    return JOURNAL


@app.post("/api/journal")
def create_journal(body: JournalCreate):
    item = body.model_dump()
    JOURNAL.insert(0, item)
    return {"ok": True, "item": item}


@app.websocket("/ws/comments/{channel}")
async def comments_ws(websocket: WebSocket, channel: str):
    if channel not in CONNECTIONS:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    CONNECTIONS[channel].append(websocket)
    await websocket.send_json({"type": "hello", "channel": channel})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in CONNECTIONS[channel]:
            CONNECTIONS[channel].remove(websocket)
