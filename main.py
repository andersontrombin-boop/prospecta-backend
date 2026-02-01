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
import sqlite3
import requests
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

# =========================
# CONFIG ENV
# =========================
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "SENHA_FORTE123")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
DB_PATH = os.getenv("DB_PATH", "app.db")
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))

# =========================
# FASTAPI
# =========================
app = FastAPI(
    title="Prospecta Assinaturas",
    version="1.0.0"
)

# =========================
# DATABASE
# =========================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT UNIQUE,
            expires_at TEXT,
            active INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT,
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
    api_key: str
    license_key: str


class PixCreate(BaseModel):
    license_key: str
    amount: float
    payer_email: str  # STRING SIMPLES (SEM EmailStr)

# =========================
# HEALTH
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}

# =========================
# CREATE LICENSE (ADMIN)
# =========================
@app.post("/admin/create-license")
def create_license(data: LicenseCreate):
    if data.api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    expires_at = datetime.utcnow() + timedelta(days=DEFAULT_BILLING_DAYS)

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            "INSERT INTO licenses (license_key, expires_at, active) VALUES (?, ?, ?)",
            (data.license_key, expires_at.isoformat(), 1)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Licença já existe")
    finally:
        conn.close()

    return {
        "ok": True,
        "license_key": data.license_key,
        "expires_at": expires_at.isoformat()
    }

# =========================
# VALIDATE LICENSE
# =========================
@app.get("/license/validate")
def validate_license(key: str):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM licenses WHERE license_key = ? AND active = 1",
        (key,)
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return {"valid": False}

    if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
        return {"valid": False}

    return {
        "valid": True,
        "license_key": key,
        "expires_at": row["expires_at"]
    }

# =========================
# CREATE PIX
# =========================
@app.post("/pix/create")
def create_pix(data: PixCreate):
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Mercado Pago não configurado")

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM licenses WHERE license_key = ? AND active = 1",
        (data.license_key,)
    )
    license_row = cur.fetchone()

    if not license_row:
        conn.close()
        raise HTTPException(status_code=403, detail="Licença inválida")

    payment_payload = {
        "transaction_amount": float(data.amount),
        "description": "Prospecta Assinatura",
        "payment_method_id": "pix",
        "payer": {
            "email": data.payer_email
        }
    }

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": str(uuid.uuid4())
    }

    response = requests.post(
        "https://api.mercadopago.com/v1/payments",
        headers=headers,
        json=payment_payload,
        timeout=30
    )

    if response.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=response.text)

    payment = response.json()

    cur.execute(
        "INSERT INTO payments (payment_id, license_key, status, created_at) VALUES (?, ?, ?, ?)",
        (
            str(payment["id"]),
            data.license_key,
            payment["status"],
            datetime.utcnow().isoformat()
        )
    )

    conn.commit()
    conn.close()

    tx = payment["point_of_interaction"]["transaction_data"]

    return {
        "payment_id": payment["id"],
        "status": payment["status"],
        "qr_code": tx.get("qr_code"),
        "qr_code_base64": tx.get("qr_code_base64"),
        "ticket_url": tx.get("ticket_url")
    }

# =========================
# MERCADO PAGO WEBHOOK
# =========================
@app.post("/mp/webhook")
async def mp_webhook(request: Request):
    payload = await request.json()
    payment_id = request.query_params.get("data.id")

    if not payment_id:
        return {"ok": True}

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}"
    }

    response = requests.get(
        f"https://api.mercadopago.com/v1/payments/{payment_id}",
        headers=headers,
        timeout=30
    )

    if response.status_code != 200:
        return {"ok": False}

    payment = response.json()
    status = payment.get("status")

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "UPDATE payments SET status = ? WHERE payment_id = ?",
        (status, str(payment_id))
    )

    if status == "approved":
        cur.execute(
            """
            UPDATE licenses
            SET expires_at = ?
            WHERE license_key = (
                SELECT license_key FROM payments WHERE payment_id = ?
            )
            """,
            (
                (datetime.utcnow() + timedelta(days=DEFAULT_BILLING_DAYS)).isoformat(),
                str(payment_id)
            )
        )

    conn.commit()
    conn.close()

    return {"ok": True}

# =========================
# RUN
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        log_config=None,
        use_colors=False
    )
