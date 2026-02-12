# =========================
# CORREÇÃO PYINSTALLER / STDOUT
# =========================
import sys
import os

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# =========================
# IMPORTS
# =========================
import uuid
import secrets
import requests
from datetime import datetime, timedelta
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from sqlalchemy import create_engine, text

# =========================
# CONFIG ENV
# =========================
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not ADMIN_API_KEY:
    raise RuntimeError("ADMIN_API_KEY não configurada no Render.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não configurada no Render.")

# =========================
# FASTAPI
# =========================
app = FastAPI(title="Prospecta Assinaturas", version="2.0.0")

# =========================
# DATABASE (POSTGRES)
# =========================
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

def init_db():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS licenses (
                id SERIAL PRIMARY KEY,
                license_key TEXT UNIQUE,
                expires_at TEXT,
                active INTEGER,
                plan TEXT DEFAULT 'monthly',
                issued_at TEXT,
                revoked INTEGER DEFAULT 0,
                revoked_at TEXT,
                revoke_reason TEXT,
                device_id TEXT,
                activated_at TEXT
            );
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                payment_id TEXT,
                license_key TEXT,
                status TEXT,
                created_at TEXT
            );
        """))

init_db()

# =========================
# HELPERS
# =========================
def utcnow():
    return datetime.utcnow()

def compute_expiration(plan: Literal["trial", "monthly"]) -> datetime:
    if plan == "trial":
        return utcnow() + timedelta(hours=48)
    return utcnow() + timedelta(days=DEFAULT_BILLING_DAYS)

def gen_license_key() -> str:
    return secrets.token_urlsafe(18)

# =========================
# MODELS
# =========================
PlanType = Literal["trial", "monthly"]

class LicenseCreate(BaseModel):
    api_key: str
    plan: PlanType
    license_key: Optional[str] = None

# =========================
# HEALTH
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}

# =========================
# CREATE LICENSE
# =========================
@app.post("/admin/create-license")
def create_license(data: LicenseCreate):
    if data.api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    issued_at = utcnow()
    expires_at = compute_expiration(data.plan)

    license_key = data.license_key or gen_license_key()

    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO licenses (
                        license_key, expires_at, active, plan, issued_at
                    )
                    VALUES (:key, :exp, 1, :plan, :issued)
                """),
                {
                    "key": license_key,
                    "exp": expires_at.isoformat(),
                    "plan": data.plan,
                    "issued": issued_at.isoformat()
                }
            )
    except Exception as e:
        raise HTTPException(status_code=400, detail="Licença já existe")

    return {
        "ok": True,
        "license_key": license_key,
        "plan": data.plan,
        "expires_at": expires_at.isoformat()
    }

# =========================
# LIST LICENSES
# =========================
@app.get("/admin/licenses")
def list_licenses(api_key: str):
    if api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM licenses ORDER BY id DESC")).mappings().all()

    return {"licenses": [dict(r) for r in rows]}

# =========================
# VALIDATE LICENSE
# =========================
@app.get("/license/validate")
def validate_license(key: str, device_id: str):
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM licenses WHERE license_key = :key"),
            {"key": key}
        ).mappings().first()

        if not row:
            return {"valid": False}

        if row["device_id"] is None:
            conn.execute(
                text("UPDATE licenses SET device_id = :device WHERE license_key = :key"),
                {"device": device_id, "key": key}
            )
            return {"valid": True, "bound": True}

        if row["device_id"] != device_id:
            return {"valid": False, "reason": "device_mismatch"}

        return {"valid": True}

# =========================
# RUN LOCAL
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
    )
