import os
import sqlite3
import uuid
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
        mp_payment_id TEXT PRIMARY KEY,
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
    except Exception:
        return None


def mp_headers(idempotency_key: Optional[str] = None):
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Mercado Pago não configurado (MP_ACCESS_TOKEN vazio).")

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    # MP pode exigir idempotency em alguns cenários
    headers["X-Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
    return headers


def mp_get_payment(payment_id: str) -> dict:
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    r = requests.get(url, headers=mp_headers())
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Erro consultando pagamento MP: {r.status_code} - {r.text}")
    return r.json()


def extend_license(license_key: str, days: int):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM licenses WHERE license_key=?", (license_key,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Licença não encontrada")

    paid_until = parse_iso(row["paid_until"])
    base = paid_until if (paid_until and paid_until > now()) else now()
    new_paid_until = base + timedelta(days=days)

    cur.execute("""
        UPDATE licenses
        SET status=?, paid_until=?
        WHERE license_key=?
    """, ("active", iso(new_paid_until), license_key))

    conn.commit()
    conn.close()
    return new_paid_until


# =========================
# ROTAS BÁSICAS
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/admin/create-license")
def create_license(body: LicenseCreate, request: Request):
    # Autorização via header x-api-key
    if request.headers.get("x-api-key") != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO licenses
        (license_key, status, paid_until, created_at)
        VALUES (?, ?, ?, ?)
    """, (body.license_key, "blocked", "", iso(now())))

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


# =========================
# PIX CREATE
# =========================
@app.post("/pix/create")
def create_pix(body: PixCreate):
    # cria pagamento Pix no MP
    payload = {
        "transaction_amount": float(body.amount),
        "description": f"Licença {body.license_key}",
        "payment_method_id": "pix",
        "payer": {"email": "comprador@teste.com"},
        # ajuda a você rastrear e vincular
        "external_reference": body.license_key,
        # opcional: expiração do qr (minutos)
        # "date_of_expiration": (now() + timedelta(minutes=30)).isoformat()
    }

    r = requests.post(
        "https://api.mercadopago.com/v1/payments",
        headers=mp_headers(),  # já inclui X-Idempotency-Key
        json=payload,
        timeout=60,
    )

    if r.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=r.text)

    data = r.json()

    payment_id = str(data.get("id"))
    status = data.get("status")

    # salva no banco
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO payments (mp_payment_id, license_key, status, created_at)
        VALUES (?, ?, ?, ?)
    """, (payment_id, body.license_key, status, iso(now())))
    conn.commit()
    conn.close()

    # retorna dados do QR / ticket
    pix_info = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    return {
        "mp_payment_id": payment_id,
        "status": status,
        "qr_code": pix_info.get("qr_code"),
        "qr_code_base64": pix_info.get("qr_code_base64"),
        "ticket_url": pix_info.get("ticket_url"),
    }


# =========================
# WEBHOOK MERCADO PAGO
# =========================
@app.post("/mp/webhook")
async def mp_webhook(request: Request):
    """
    Mercado Pago chama algo como:
    POST /mp/webhook?data.id=XXXXX&type=payment
    """
    # 1) pegar payment_id
    payment_id = request.query_params.get("data.id")
    event_type = request.query_params.get("type")

    # fallback: às vezes vem no body
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    if not payment_id:
        payment_id = (
            body.get("data", {}).get("id")
            or body.get("id")
            or body.get("data_id")
        )

    if not payment_id:
        # responde 200 pra não ficar re-tentando infinito
        return {"ok": True, "ignored": "no_payment_id"}

    # Só tratar evento de pagamento
    if event_type and event_type != "payment":
        return {"ok": True, "ignored": f"type={event_type}"}

    # 2) consultar pagamento no MP (fonte confiável)
    mp = mp_get_payment(str(payment_id))
    status = mp.get("status", "")
    external_reference = mp.get("external_reference")  # deve ser license_key

    # 3) achar a license_key
    license_key = external_reference

    if not license_key:
        # tenta pelo nosso banco de payments
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM payments WHERE mp_payment_id=?", (str(payment_id),))
        prow = cur.fetchone()
        conn.close()
        if prow:
            license_key = prow["license_key"]

    if not license_key:
        return {"ok": True, "ignored": "no_license_key"}

    # 4) gravar/atualizar pagamento
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO payments (mp_payment_id, license_key, status, created_at)
        VALUES (?, ?, ?, ?)
    """, (str(payment_id), license_key, status, iso(now())))
    conn.commit()
    conn.close()

    # 5) se aprovado, ativa/estende licença
    if status == "approved":
        new_until = extend_license(license_key, DEFAULT_BILLING_DAYS)
        return {"ok": True, "payment_id": str(payment_id), "status": status, "license_key": license_key, "paid_until": iso(new_until)}

    return {"ok": True, "payment_id": str(payment_id), "status": status, "license_key": license_key}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
