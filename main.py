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
import secrets
from datetime import datetime, timedelta
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from sqlalchemy import create_engine, text

# ✅ força o driver psycopg3 estar disponível (e evita cair em psycopg2)
import psycopg  # noqa: F401


# =========================
# CONFIG ENV
# =========================
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not ADMIN_API_KEY or len(ADMIN_API_KEY.strip()) < 8:
    raise RuntimeError("ADMIN_API_KEY não configurada (ou muito curta). Configure no Render > Environment.")

if not DATABASE_URL or len(DATABASE_URL.strip()) < 20:
    raise RuntimeError("DATABASE_URL não configurada. Cole a connection string do Supabase no Render > Environment.")

# ✅ SQLAlchemy + psycopg3:
# se vier "postgresql://", trocamos para "postgresql+psycopg://"
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

# =========================
# FASTAPI
# =========================
app = FastAPI(title="Prospecta Assinaturas", version="2.1.0")

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
    return secrets.token_urlsafe(18).replace("-", "").replace("_", "")

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

    license_key = (data.license_key or "").strip() or gen_license_key()

    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO licenses (license_key, expires_at, active, plan, issued_at, revoked)
                    VALUES (:key, :exp, 1, :plan, :issued, 0)
                """),
                {
                    "key": license_key,
                    "exp": expires_at.isoformat(),
                    "plan": data.plan,
                    "issued": issued_at.isoformat(),
                }
            )
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg:
            raise HTTPException(status_code=400, detail="Licença já existe")
        raise HTTPException(status_code=500, detail=f"Erro ao criar licença: {str(e)}")

    return {
        "ok": True,
        "license_key": license_key,
        "plan": data.plan,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
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

    return {"ok": True, "items": [dict(r) for r in rows]}

# =========================
# VALIDATE LICENSE (trava por PC)
# =========================
@app.get("/license/validate")
def validate_license(key: str, device_id: str):
    key = (key or "").strip()
    device_id = (device_id or "").strip()

    if not key:
        return {"valid": False, "reason": "missing_key"}
    if not device_id or len(device_id) < 8:
        return {"valid": False, "reason": "missing_device_id"}

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM licenses WHERE license_key = :key"),
            {"key": key}
        ).mappings().first()

        if not row:
            return {"valid": False, "reason": "not_found"}

        if int(row.get("active") or 0) != 1:
            return {"valid": False, "reason": "inactive"}

        if int(row.get("revoked") or 0) == 1:
            return {"valid": False, "reason": "revoked"}

        exp = row.get("expires_at")
        if not exp:
            return {"valid": False, "reason": "expired"}

        # valida expiração
        try:
            exp_dt = datetime.fromisoformat(exp)
        except Exception:
            return {"valid": False, "reason": "expired"}

        if exp_dt < utcnow():
            return {"valid": False, "reason": "expired"}

        stored_device = (row.get("device_id") or "").strip()

        # primeira ativação -> vincula
        if not stored_device:
            conn.execute(
                text("UPDATE licenses SET device_id = :d, activated_at = :a WHERE license_key = :k"),
                {"d": device_id, "a": utcnow().isoformat(), "k": key}
            )
            return {"valid": True, "bound": True}

        if stored_device != device_id:
            return {"valid": False, "reason": "device_mismatch"}

        return {"valid": True, "bound": False}

# =========================
# WEBHOOK (mantive só o esqueleto pra não quebrar rota)
# =========================
@app.post("/mp/webhook")
async def mp_webhook(request: Request):
    # Se você ainda usa isso, depois a gente reimplementa completo.
    _ = await request.json()
    return {"ok": True}

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
