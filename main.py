import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, Dict

import requests
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================
load_dotenv()

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
DB_PATH = os.getenv("DB_PATH", "app.db")

DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))

app = FastAPI(title="Prospecta Assinaturas", version="1.0.0")


# =========================
# BANCO DE DADOS
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
    # ✅ Para funcionar no Swagger sem header
    api_key: Optional[str] = None


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


def get_admin_key(request: Request, body_api_key: Optional[str]) -> str:
    # ✅ Aceita header OU body
    return (request.headers.get("x-api-key") or body_api_key or "").strip()


def ensure_admin(request: Request, body_api_key: Optional[str]):
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY não configurado no Render")

    sent = get_admin_key(request, body_api_key)
    if sent != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")


# =========================
# ROTAS
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/admin/create-license")
def create_license(body: LicenseCreate, request: Request):
    ensure_admin(request, body.api_key)

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
        raise HTTPException(status_code=500, detail="Mercado Pago não configurado (MP_ACCESS_TOKEN vazio)")

    # (Opcional) validar licença existe
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM licenses WHERE license_key=?", (body.license_key,))
    lic = cur.fetchone()
    conn.close()

    if not lic:
        raise HTTPException(status_code=404, detail="license_key não encontrada. Crie a licença antes.")

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        # ✅ obrigatório pro MP não reclamar (idempotência)
        "X-Idempotency-Key": str(uuid.uuid4()),
    }

    payload = {
        "transaction_amount": float(body.amount),
        "description": f"Licença {body.license_key}",
        "payment_method_id": "pix",
        # MP costuma aceitar sem payer em alguns casos, mas é mais estável mandar um email dummy
        "payer": {
            "email": "test_user_123@test.com"
        }
    }

    resp = requests.post(
        "https://api.mercadopago.com/v1/payments",
        headers=headers,
        json=payload,
        timeout=60
    )

    # Mercado Pago manda erros detalhados no body
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=resp.text)

    data: Dict[str, Any] = resp.json()

    tx = (((data.get("point_of_interaction") or {}).get("transaction_data")) or {})
    qr_code = tx.get("qr_code")
    qr_code_base64 = tx.get("qr_code_base64")
    ticket_url = tx.get("ticket_url")

    return {
        "mp_payment_id": data.get("id"),
        "status": data.get("status"),
        "license_key": body.license_key,
        "amount": body.amount,
        "qr_code": qr_code,
        "qr_code_base64": qr_code_base64,
        "ticket_url": ticket_url,
        "raw": data if not (qr_code or ticket_url) else None
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
