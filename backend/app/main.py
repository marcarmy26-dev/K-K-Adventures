"""
Keralee & Kayden Backend — v3
JWT auth · host/viewer roles · PostgreSQL · Cloudflare Stream (WHIP/WHEP)
"""

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, Session

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "")
HOST_USERNAME = os.getenv("HOST_USERNAME", "admin")
HOST_PASSWORD = os.getenv("HOST_PASSWORD", "FamilyOnly123")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24
CHANNELS = ["studio-keralee", "studio-kayden", "studio-together"]

# ── Database ──────────────────────────────────────────────────────────
engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None
SessionLocal = sessionmaker(bind=engine) if engine else None
Base = declarative_base()


class UserRow(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="viewer")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class CommentRow(Base):
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True, index=True)
    channel = Column(String(50), nullable=False, index=True)
    author = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class JournalRow(Base):
    __tablename__ = "journal_entries"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    date = Column(String(20), nullable=False)
    host = Column(String(50), nullable=False)
    category = Column(String(50), nullable=False)
    story = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class StreamRow(Base):
    __tablename__ = "streams"
    id = Column(Integer, primary_key=True, index=True)
    channel = Column(String(50), unique=True, nullable=False, index=True)
    cf_input_uid = Column(String(255), nullable=True)
    cf_whip_url = Column(Text, nullable=True)
    cf_whep_url = Column(Text, nullable=True)
    is_live = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


if engine:
    # One-time fix: drop old streams table with Mux columns, recreate with Cloudflare columns
    try:
        from sqlalchemy import text as _text, inspect as _inspect
        _insp = _inspect(engine)
        if _insp.has_table("streams"):
            _cols = [c["name"] for c in _insp.get_columns("streams")]
            if "cf_input_uid" not in _cols:
                with engine.connect() as _conn:
                    _conn.execute(_text("DROP TABLE streams"))
                    _conn.commit()
    except Exception:
        pass
    Base.metadata.create_all(bind=engine)

# ── Auth utilities ────────────────────────────────────────────────────
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


def get_db():
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_token(username: str, role: str) -> str:
    payload = {
        "sub": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return decode_token(creds.credentials)


def require_host(user: dict = Depends(get_current_user)):
    if user.get("role") != "host":
        raise HTTPException(status_code=403, detail="Host access required")
    return user


# ── Seed host account on startup ──────────────────────────────────────
def seed_host():
    if not SessionLocal:
        return
    db = SessionLocal()
    try:
        existing = db.query(UserRow).filter(UserRow.username == HOST_USERNAME).first()
        if not existing:
            db.add(UserRow(
                username=HOST_USERNAME,
                password_hash=pwd_ctx.hash(HOST_PASSWORD),
                role="host",
            ))
            db.commit()
    finally:
        db.close()


# ── Profanity filter ─────────────────────────────────────────────────
PROFANITY_PATTERNS = [
    re.compile(r"\bdamn\b", re.I),
    re.compile(r"\bhell\b", re.I),
    re.compile(r"\bcrap\b", re.I),
    re.compile(r"\bwtf\b", re.I),
    re.compile(r"\bidiot\b", re.I),
    re.compile(r"\bstupid\b", re.I),
    re.compile(r"\bshut up\b", re.I),
]


def contains_profanity(text: str) -> bool:
    return any(p.search(text) for p in PROFANITY_PATTERNS)


# ── WebSocket state ──────────────────────────────────────────────────
CONNECTIONS: Dict[str, List[WebSocket]] = {ch: [] for ch in CHANNELS}


async def broadcast(channel: str, payload: dict):
    dead = []
    for ws in CONNECTIONS.get(channel, []):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in CONNECTIONS[channel]:
            CONNECTIONS[channel].remove(ws)


# ── Pydantic schemas ─────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    username: str
    password: str


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


# ── Cloudflare Stream helpers ─────────────────────────────────────────
CF_API = "https://api.cloudflare.com/client/v4"


def cf_headers() -> dict:
    return {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}


async def cf_create_live_input(name: str) -> dict:
    """Create a Cloudflare Stream live input with WHIP/WHEP URLs."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{CF_API}/accounts/{CF_ACCOUNT_ID}/stream/live_inputs",
            headers=cf_headers(),
            json={
                "meta": {"name": name},
                "recording": {"mode": "automatic"},
            },
        )
        data = resp.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            detail = errors[0].get("message", str(data)) if errors else str(data)
            raise HTTPException(status_code=502, detail=f"Cloudflare error: {detail}")
        return data["result"]


async def cf_delete_live_input(input_uid: str):
    """Delete a Cloudflare Stream live input."""
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{CF_API}/accounts/{CF_ACCOUNT_ID}/stream/live_inputs/{input_uid}",
            headers=cf_headers(),
        )


# ── FastAPI app ──────────────────────────────────────────────────────
app = FastAPI(title="Keralee & Kayden Backend", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    seed_host()
    # Reset stale live states from previous server runs
    if SessionLocal:
        db = SessionLocal()
        try:
            db.query(StreamRow).filter(StreamRow.is_live == True).update({"is_live": False})
            db.commit()
        except Exception:
            pass
        finally:
            db.close()


# ── Health ────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"ok": True, "service": "kk-fastapi", "version": "3.0.0", "channels": CHANNELS}


# ── Auth endpoints ────────────────────────────────────────────────────
@app.post("/api/auth/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    if len(body.username.strip()) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    existing = db.query(UserRow).filter(UserRow.username == body.username.strip()).first()
    if existing:
        raise HTTPException(status_code=409, detail="Username already taken")
    user = UserRow(
        username=body.username.strip(),
        password_hash=pwd_ctx.hash(body.password),
        role="viewer",
    )
    db.add(user)
    db.commit()
    token = create_token(user.username, user.role)
    return {"ok": True, "token": token, "role": user.role, "username": user.username}


@app.post("/api/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(UserRow).filter(UserRow.username == body.username.strip()).first()
    if not user or not pwd_ctx.verify(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_token(user.username, user.role)
    return {"ok": True, "token": token, "role": user.role, "username": user.username}


@app.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {"ok": True, "username": user["sub"], "role": user["role"]}


# ── Comments (DB-backed) ─────────────────────────────────────────────
@app.get("/api/comments/{channel}")
def list_comments(channel: str, db: Session = Depends(get_db)):
    if channel not in CHANNELS:
        raise HTTPException(status_code=404, detail="Unknown channel")
    rows = db.query(CommentRow).filter(CommentRow.channel == channel).order_by(CommentRow.created_at.desc()).limit(200).all()
    return [
        {"id": r.id, "author": r.author, "message": r.message, "timestamp": r.created_at.isoformat() + "Z"}
        for r in rows
    ]


@app.post("/api/comments/{channel}")
async def create_comment(channel: str, body: CommentCreate, db: Session = Depends(get_db)):
    if channel not in CHANNELS:
        raise HTTPException(status_code=404, detail="Unknown channel")
    if contains_profanity(body.message):
        raise HTTPException(status_code=400, detail="Comment blocked by family-safe filter")
    if contains_profanity(body.author):
        raise HTTPException(status_code=400, detail="Display name blocked by family-safe filter")
    row = CommentRow(channel=channel, author=body.author.strip(), message=body.message.strip())
    db.add(row)
    db.commit()
    db.refresh(row)
    item = {"id": row.id, "author": row.author, "message": row.message, "timestamp": row.created_at.isoformat() + "Z"}
    await broadcast(channel, {"type": "comment_created", "channel": channel, "item": item})
    return {"ok": True, "item": item}


@app.delete("/api/comments/{channel}/{comment_id}")
async def delete_comment(channel: str, comment_id: int, user: dict = Depends(require_host), db: Session = Depends(get_db)):
    row = db.query(CommentRow).filter(CommentRow.id == comment_id, CommentRow.channel == channel).first()
    if not row:
        raise HTTPException(status_code=404, detail="Comment not found")
    db.delete(row)
    db.commit()
    await broadcast(channel, {"type": "comment_deleted", "channel": channel, "comment_id": comment_id})
    return {"ok": True}


@app.delete("/api/comments/{channel}")
async def clear_comments(channel: str, user: dict = Depends(require_host), db: Session = Depends(get_db)):
    if channel not in CHANNELS:
        raise HTTPException(status_code=404, detail="Unknown channel")
    db.query(CommentRow).filter(CommentRow.channel == channel).delete()
    db.commit()
    await broadcast(channel, {"type": "comments_cleared", "channel": channel})
    return {"ok": True}


# ── Journal (DB-backed) ──────────────────────────────────────────────
@app.get("/api/journal")
def list_journal(db: Session = Depends(get_db)):
    rows = db.query(JournalRow).order_by(JournalRow.created_at.desc()).limit(100).all()
    return [
        {"id": r.id, "title": r.title, "date": r.date, "host": r.host, "category": r.category, "story": r.story}
        for r in rows
    ]


@app.post("/api/journal")
def create_journal(body: JournalCreate, user: dict = Depends(require_host), db: Session = Depends(get_db)):
    row = JournalRow(
        title=body.title.strip(), date=body.date, host=body.host,
        category=body.category, story=body.story.strip(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "item": {"id": row.id, "title": row.title, "date": row.date, "host": row.host, "category": row.category, "story": row.story}}


@app.delete("/api/journal/{entry_id}")
def delete_journal(entry_id: int, user: dict = Depends(require_host), db: Session = Depends(get_db)):
    row = db.query(JournalRow).filter(JournalRow.id == entry_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Entry not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ── Streaming (Cloudflare Stream WHIP/WHEP) ───────────────────────────
@app.get("/api/streams/{channel}")
def get_stream_status(channel: str, db: Session = Depends(get_db)):
    """Public: check if a channel is live and get WHEP playback URL."""
    if channel not in CHANNELS:
        raise HTTPException(status_code=404, detail="Unknown channel")
    row = db.query(StreamRow).filter(StreamRow.channel == channel).first()
    if not row or not row.is_live:
        return {"channel": channel, "is_live": False, "whep_url": None}
    return {"channel": channel, "is_live": True, "whep_url": row.cf_whep_url}


@app.post("/api/streams/{channel}/start")
async def start_stream(channel: str, user: dict = Depends(require_host), db: Session = Depends(get_db)):
    """Host only: start streaming. Always provisions a fresh Cloudflare live input."""
    if channel not in CHANNELS:
        raise HTTPException(status_code=404, detail="Unknown channel")
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise HTTPException(status_code=500, detail="Cloudflare Stream credentials not configured")

    row = db.query(StreamRow).filter(StreamRow.channel == channel).first()

    # Always create a fresh live input so stale WHIP/WHEP sessions do not get reused.
    if row and row.cf_input_uid:
        try:
            await cf_delete_live_input(row.cf_input_uid)
        except Exception:
            pass
        row.cf_input_uid = None
        row.cf_whip_url = None
        row.cf_whep_url = None
        row.is_live = False
        row.updated_at = datetime.now(timezone.utc)
        db.commit()

    result = await cf_create_live_input(channel)
    whip_url = result.get("webRTC", {}).get("url", "")
    whep_url = result.get("webRTCPlayback", {}).get("url", "")

    if not row:
        row = StreamRow(channel=channel)
        db.add(row)

    row.cf_input_uid = result["uid"]
    row.cf_whip_url = whip_url
    row.cf_whep_url = whep_url
    row.is_live = True
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)

    await broadcast(channel, {"type": "stream_started", "channel": channel, "whep_url": whep_url})

    return {
        "ok": True,
        "channel": channel,
        "whip_url": whip_url,
        "whep_url": whep_url,
    }


@app.post("/api/streams/{channel}/stop")
async def stop_stream(channel: str, user: dict = Depends(require_host), db: Session = Depends(get_db)):
    """Host only: end the live stream and clear the current Cloudflare input."""
    if channel not in CHANNELS:
        raise HTTPException(status_code=404, detail="Unknown channel")
    row = db.query(StreamRow).filter(StreamRow.channel == channel).first()
    if not row:
        return {"ok": True, "detail": "No stream found"}
    if row.cf_input_uid:
        try:
            await cf_delete_live_input(row.cf_input_uid)
        except Exception:
            pass
    row.cf_input_uid = None
    row.cf_whip_url = None
    row.cf_whep_url = None
    row.is_live = False
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    await broadcast(channel, {"type": "stream_stopped", "channel": channel})
    return {"ok": True}


# ── WebSocket for live comments ──────────────────────────────────────
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
