import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================
load_dotenv()

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
BASE_URL = os.getenv("BASE_URL", "")
DB_PATH = os.getenv("DB_PATH", "app.db")
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))

app = FastAPI(title="Prospecta Assinaturas", version="1.0.0")

# =========================
# DATABASE
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS licenses (
        license_key TEXT PRIMARY KEY,
        status TEXT,
        paid_until TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        mp_payment_id TEXT,
        license_key TEXT,
        status TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


init_db()

# =========================
# MODELS
# =========================
class LicenseCreate(BaseModel):
    license_key: str


class PixCreate(BaseModel):
    license_key: str
    amount: float


# =========================
# HELPERS
# =========================
def now():
    return datetime.now(timezone.utc)


def iso(dt: datetime):
    return dt.isoformat()


def parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except:
        return None


# =========================
# ROUTES
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/admin/create-license")
def create_license(body: LicenseCreate, request: Request):
    if request.headers.get("x-api-key") != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO licenses
        (license_key, status, paid_until, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        body.license_key,
        "blocked",
        "",
        iso(now())
    ))

    conn.commit()
    conn.close()

    return {"ok": True, "license_key": body.license_key}


@app.get("/license/validate")
def validate_license(key: str):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM licenses WHERE license_key=?", (key,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return {"valid": False, "reason": "not_found"}

    paid_until = parse_iso(row["paid_until"])

    if row["status"] != "active":
        return {"valid": False, "reason": "inactive"}

    if paid_until and now() <= paid_until:
        return {"valid": True, "paid_until": row["paid_until"]}

    return {"valid": False, "reason": "expired"}


@app.post("/pix/create")
def create_pix(body: PixCreate):
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Mercado Pago não configurado")

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "transaction_amount": body.amount,
        "description": f"Licença Prospecta {body.license_key}",
        "payment_method_id": "pix",
        "payer": {
            "email": "pagador@prospecta.app"
        }
    }

    response = requests.post(
        "https://api.mercadopago.com/v1/payments",
        headers=headers,
        json=payload
    )

    if response.status_code not in (200, 201):
        raise HTTPException(
            status_code=500,
            detail=response.text
        )

    data = response.json()

    return {
        "payment_id": data["id"],
        "qr_code": data["point_of_interaction"]["transaction_data"]["qr_code"],
        "qr_code_base64": data["point_of_interaction"]["transaction_data"]["qr_code_base64"]
    }
