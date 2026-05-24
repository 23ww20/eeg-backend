from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import time
import hashlib
import secrets
import json
from datetime import datetime

app = FastAPI(title="EEG Monitor API", version="1.0.0")

# 允许所有跨域（小程序需要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)

# ===================== 数据库初始化 =====================
def get_db():
    conn = sqlite3.connect("eeg_data.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            token TEXT,
            created_at REAL DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS eeg_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stage TEXT NOT NULL,
            theta REAL DEFAULT 0,
            alpha REAL DEFAULT 0,
            beta REAL DEFAULT 0,
            attention INTEGER DEFAULT 0,
            meditation INTEGER DEFAULT 0,
            poor_signal INTEGER DEFAULT 0,
            timestamp REAL NOT NULL,
            created_at REAL DEFAULT (unixepoch()),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL,
            summary TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ===================== 数据模型 =====================
class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class EEGDataRequest(BaseModel):
    stage: str
    theta: float = 0
    alpha: float = 0
    beta: float = 0
    attention: int = 0
    meditation: int = 0
    poor_signal: int = 0
    timestamp: Optional[float] = None

class EEGBatchRequest(BaseModel):
    records: List[EEGDataRequest]

# ===================== 认证工具 =====================
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="未提供Token")
    token = credentials.credentials
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="Token无效或已过期")
    return dict(user)

# ===================== 用户接口 =====================
@app.post("/api/register")
def register(req: RegisterRequest):
    if len(req.username) < 2 or len(req.password) < 6:
        raise HTTPException(status_code=400, detail="用户名至少2位，密码至少6位")
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (req.username, hash_password(req.password))
        )
        conn.commit()
        return {"success": True, "message": "注册成功"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="用户名已存在")
    finally:
        conn.close()

@app.post("/api/login")
def login(req: LoginRequest):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ? AND password_hash = ?",
        (req.username, hash_password(req.password))
    ).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = secrets.token_hex(32)
    conn.execute("UPDATE users SET token = ? WHERE id = ?", (token, user["id"]))
    conn.commit()
    conn.close()
    return {"success": True, "token": token, "username": req.username}

@app.get("/api/me")
def get_me(user=Depends(get_current_user)):
    return {"username": user["username"], "id": user["id"]}

# ===================== 脑电数据接口 =====================
@app.post("/api/eeg/upload")
def upload_eeg(req: EEGDataRequest, user=Depends(get_current_user)):
    """Python端每3秒调用一次，上传单条数据"""
    ts = req.timestamp or time.time()
    conn = get_db()
    conn.execute(
        """INSERT INTO eeg_records
           (user_id, stage, theta, alpha, beta, attention, meditation, poor_signal, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user["id"], req.stage, req.theta, req.alpha, req.beta,
         req.attention, req.meditation, req.poor_signal, ts)
    )
    conn.commit()
    conn.close()
    return {"success": True}

@app.post("/api/eeg/batch")
def upload_batch(req: EEGBatchRequest, user=Depends(get_current_user)):
    """批量上传（断网重连后补传用）"""
    conn = get_db()
    for r in req.records:
        ts = r.timestamp or time.time()
        conn.execute(
            """INSERT INTO eeg_records
               (user_id, stage, theta, alpha, beta, attention, meditation, poor_signal, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user["id"], r.stage, r.theta, r.alpha, r.beta,
             r.attention, r.meditation, r.poor_signal, ts)
        )
    conn.commit()
    conn.close()
    return {"success": True, "count": len(req.records)}

@app.get("/api/eeg/latest")
def get_latest(user=Depends(get_current_user)):
    """小程序首页：获取最新一条状态"""
    conn = get_db()
    row = conn.execute(
        """SELECT * FROM eeg_records WHERE user_id = ?
           ORDER BY timestamp DESC LIMIT 1""",
        (user["id"],)
    ).fetchone()
    conn.close()
    if not row:
        return {"data": None}
    return {"data": dict(row)}

@app.get("/api/eeg/today")
def get_today(user=Depends(get_current_user)):
    """小程序今日页：今天的所有记录"""
    today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    conn = get_db()
    rows = conn.execute(
        """SELECT stage, theta, alpha, beta, attention, meditation, timestamp
           FROM eeg_records WHERE user_id = ? AND timestamp >= ?
           ORDER BY timestamp ASC""",
        (user["id"], today_start)
    ).fetchall()
    conn.close()
    records = [dict(r) for r in rows]

    # 今日统计
    if records:
        stages = [r["stage"] for r in records]
        stage_counts = {}
        for s in stages:
            stage_counts[s] = stage_counts.get(s, 0) + 1
        dominant = max(stage_counts, key=stage_counts.get)
        avg_attention  = sum(r["attention"]  for r in records) / len(records)
        avg_meditation = sum(r["meditation"] for r in records) / len(records)
    else:
        dominant = "--"
        avg_attention = avg_meditation = 0

    return {
        "records": records,
        "summary": {
            "count": len(records),
            "dominant_stage": dominant,
            "avg_attention": round(avg_attention, 1),
            "avg_meditation": round(avg_meditation, 1),
        }
    }

@app.get("/api/eeg/history")
def get_history(days: int = 7, user=Depends(get_current_user)):
    """小程序历史页：最近N天每天的汇总"""
    conn = get_db()
    result = []
    for i in range(days - 1, -1, -1):
        day_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp() - i * 86400
        day_end   = day_start + 86400
        rows = conn.execute(
            """SELECT stage, attention, meditation FROM eeg_records
               WHERE user_id = ? AND timestamp >= ? AND timestamp < ?""",
            (user["id"], day_start, day_end)
        ).fetchall()
        rows = [dict(r) for r in rows]
        date_str = datetime.fromtimestamp(day_start).strftime("%m/%d")
        if rows:
            stages = [r["stage"] for r in rows]
            stage_counts = {}
            for s in stages:
                stage_counts[s] = stage_counts.get(s, 0) + 1
            result.append({
                "date": date_str,
                "count": len(rows),
                "dominant_stage": max(stage_counts, key=stage_counts.get),
                "avg_attention":  round(sum(r["attention"]  for r in rows) / len(rows), 1),
                "avg_meditation": round(sum(r["meditation"] for r in rows) / len(rows), 1),
                "stage_counts": stage_counts,
            })
        else:
            result.append({"date": date_str, "count": 0, "dominant_stage": "--",
                           "avg_attention": 0, "avg_meditation": 0, "stage_counts": {}})
    conn.close()
    return {"history": result}

@app.get("/api/health")
def health():
    return {"status": "ok", "time": time.time()}

