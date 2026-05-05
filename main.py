import os
import secrets
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal, Any, Dict, List

import psycopg
from psycopg.errors import UniqueViolation
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# =========================
# CONFIG (SEM LICENÇA)
# =========================
ADMIN_API_KEY = (os.getenv("ADMIN_API_KEY") or "dev_key").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connect_db():
    raise Exception("DB desativado")


def fetchone_dict(cur) -> Optional[Dict[str, Any]]:
    row = cur.fetchone()
    if not row:
        return None
    cols = [d.name for d in cur.description]
    return dict(zip(cols, row))


def fetchall_dict(cur) -> List[Dict[str, Any]]:
    rows = cur.fetchall() or []
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def gen_license_key() -> str:
    return secrets.token_urlsafe(18).replace("-", "").replace("_", "")


LicenseType = Literal["trial", "monthly"]
LicenseStatus = Literal["active", "blocked", "expired", "canceled"]

app = FastAPI(title="Prospecta Backend", version="2.0.2")

# =========================
# CORS
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # liberado
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# MODELS
# =========================
class AdminCreateLicense(BaseModel):
    api_key: str
    license_type: LicenseType = "trial"
    duration_hours: Optional[int] = None
    license_key: Optional[str] = None
    status: LicenseStatus = "active"
    buyer_email: Optional[str] = None


class AdminResetLicense(BaseModel):
    api_key: str
    license_key: str


class AdminRevokeLicense(BaseModel):
    api_key: str
    license_key: str
    status: LicenseStatus = "blocked"


class ActivateRequest(BaseModel):
    license_key: str = Field(..., min_length=1)
    device_id: str = Field(..., min_length=1)
    buyer_email: Optional[str] = None


# =========================
# HEALTH
# =========================
@app.get("/health")
def health():
    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@app.get("/health")
def health():
    return {"ok": True}


# =========================
# ADMIN (SEM AUTH)
# =========================
@app.post("/admin/create-license")
def admin_create_license(data: AdminCreateLicense):
    key = (data.license_key or "").strip() or gen_license_key()

    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.licenses
                      (license_key, license_type, status, duration_hours, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING license_key;
                    """,
                    (
                        key,
                        data.license_type,
                        data.status,
                        data.duration_hours or 48,
                        utcnow(),
                    ),
                )
                row = fetchone_dict(cur)
        return {"ok": True, "license": row}

    except UniqueViolation:
        raise HTTPException(status_code=400, detail="Licença já existe")


@app.get("/admin/licenses")
def admin_list_licenses():
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.licenses ORDER BY created_at DESC;")
            items = fetchall_dict(cur)
    return {"ok": True, "items": items}


# =========================
# LICENÇA DESATIVADA
# =========================
@app.post("/license/activate")
def activate_license(data: ActivateRequest):
    return {
        "ok": True,
        "valid": True,
        "license_key": data.license_key,
        "device_id": data.device_id,
        "activated_at": utcnow().isoformat(),
        "expires_at": None,
    }


@app.get("/license/validate")
def validate_license(key: str, device_id: str, buyer_email: Optional[str] = None):
    return {
        "ok": True,
        "valid": True,
        "license_key": key,
        "device_id": device_id,
        "activated_at": utcnow().isoformat(),
        "expires_at": None,
    }
