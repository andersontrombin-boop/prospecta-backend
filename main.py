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
import secrets
import requests
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Literal

# =========================
# CONFIG ENV
# =========================
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "SENHA_FORTE123")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
DB_PATH = os.getenv("DB_PATH", "app.db")

# Mantém seu padrão (30 dias) para mensal
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))

# =========================
# FASTAPI
# =========================
app = FastAPI(
    title="Prospecta Assinaturas",
    version="1.1.0"
)

# =========================
# DATABASE
# =========================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def table_has_column(conn, table: str, column: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r["name"] for r in cur.fetchall()]
    return column in cols

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

    # Migração: adiciona colunas novas se não existirem
    if not table_has_column(conn, "licenses", "plan"):
        cur.execute("ALTER TABLE licenses ADD COLUMN plan TEXT DEFAULT 'monthly'")
    if not table_has_column(conn, "licenses", "issued_at"):
        cur.execute("ALTER TABLE licenses ADD COLUMN issued_at TEXT")
    if not table_has_column(conn, "licenses", "revoked"):
        cur.execute("ALTER TABLE licenses ADD COLUMN revoked INTEGER DEFAULT 0")
    if not table_has_column(conn, "licenses", "revoked_at"):
        cur.execute("ALTER TABLE licenses ADD COLUMN revoked_at TEXT")
    if not table_has_column(conn, "licenses", "revoke_reason"):
        cur.execute("ALTER TABLE licenses ADD COLUMN revoke_reason TEXT")

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
# HELPERS
# =========================
def utcnow():
    return datetime.utcnow()

def compute_expiration(plan: Literal["trial", "monthly"]) -> datetime:
    if plan == "trial":
        return utcnow() + timedelta(hours=48)
    # monthly
    return utcnow() + timedelta(days=DEFAULT_BILLING_DAYS)

def gen_license_key() -> str:
    # chave simples, forte e curta
    return secrets.token_urlsafe(18).replace("-", "").replace("_", "")

# =========================
# MODELS
# =========================
PlanType = Literal["trial", "monthly"]

class LicenseCreate(BaseModel):
    api_key: str
    plan: PlanType  # trial=48h | monthly=30d
    license_key: Optional[str] = None  # se não mandar, gera automático

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

    plan = data.plan
    issued_at = utcnow()
    expires_at = compute_expiration(plan)

    # Se não veio license_key, gera uma
    license_key = (data.license_key or "").strip()
    if not license_key:
        license_key = gen_license_key()

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO licenses (license_key, expires_at, active, plan, issued_at, revoked, revoked_at, revoke_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                license_key,
                expires_at.isoformat(),
                1,
                plan,
                issued_at.isoformat(),
                0,
                None,
                None
            )
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Licença já existe")
    finally:
        conn.close()

    return {
        "ok": True,
        "license_key": license_key,
        "plan": plan,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat()
    }

# =========================
# VALIDATE LICENSE
# (Prospecta chama isso)
# =========================
@app.get("/license/validate")
def validate_license(key: str):
    key = (key or "").strip()
    if not key:
        return {"valid": False}

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM licenses WHERE license_key = ?",
        (key,)
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return {"valid": False}

    # precisa estar ativo e não revogado
    if int(row["active"]) != 1:
        return {"valid": False}

    # coluna revoked pode ser NULL em registros antigos (por garantia)
    revoked = row["revoked"]
    if revoked is not None and int(revoked) == 1:
        return {"valid": False}

    # expiração
    if datetime.fromisoformat(row["expires_at"]) < utcnow():
        return {"valid": False}

    return {
        "valid": True,
        "license_key": key,
        "plan": row["plan"],
        "issued_at": row["issued_at"],
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
            utcnow().isoformat()
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

    # Quando aprovado: renova como mensal (30 dias a partir de agora)
    if status == "approved":
        cur.execute(
            """
            UPDATE licenses
            SET expires_at = ?, plan = 'monthly'
            WHERE license_key = (
                SELECT license_key FROM payments WHERE payment_id = ?
            )
            """,
            (
                (utcnow() + timedelta(days=DEFAULT_BILLING_DAYS)).isoformat(),
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
