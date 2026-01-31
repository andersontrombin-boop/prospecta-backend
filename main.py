import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, Dict

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================
load_dotenv()

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").strip()
DB_PATH = os.getenv("DB_PATH", "app.db").strip()
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))

# Seu webhook no MP deve apontar para:
#   {BASE_URL}/webhook/mercadopago
WEBHOOK_PATH = "/webhook/mercadopago"

app = FastAPI(title="Prospecta Assinaturas", version="1.0.0")

MP_PAYMENTS_URL = "https://api.mercadopago.com/v1/payments"


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
    payer_email: Optional[str] = None  # opcional; se não vier, usamos um padrão


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


def mp_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def mp_get_payment(payment_id: str) -> Dict[str, Any]:
    r = requests.get(f"{MP_PAYMENTS_URL}/{payment_id}", headers=mp_headers(), timeout=30)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Erro ao consultar pagamento no MP: {r.text}")
    return r.json()


def ensure_license_exists(license_key: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO licenses (license_key, status, paid_until, created_at)
        VALUES (?, ?, ?, ?)
    """, (license_key, "blocked", "", iso(now())))
    conn.commit()
    conn.close()


def update_license_paid(license_key: str):
    paid_until_dt = now() + timedelta(days=DEFAULT_BILLING_DAYS)
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE licenses
        SET status=?, paid_until=?
        WHERE license_key=?
    """, ("active", iso(paid_until_dt), license_key))
    conn.commit()
    conn.close()
    return paid_until_dt


def upsert_payment(mp_payment_id: str, license_key: str, status: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO payments (mp_payment_id, license_key, status, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(mp_payment_id) DO UPDATE SET
            status=excluded.status
    """, (mp_payment_id, license_key, status, iso(now())))
    conn.commit()
    conn.close()


# =========================
# ROTAS
# =========================
@app.get("/")
def root():
    # evita "Not Found" na raiz
    return {"ok": True, "message": "API online. Acesse /docs para testar."}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/admin/create-license")
def create_license(body: LicenseCreate, request: Request):
    if request.headers.get("x-api-key") != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    ensure_license_exists(body.license_key)
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

    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="amount precisa ser maior que zero")

    # garante que a licença existe no DB
    ensure_license_exists(body.license_key)

    payer_email = (body.payer_email or "test_user_123456@test.com").strip()

    payload = {
        "transaction_amount": float(body.amount),
        "description": f"Prospecta Assinaturas - Licenca {body.license_key}",
        "payment_method_id": "pix",
        "payer": {"email": payer_email},
        "external_reference": body.license_key,
        "notification_url": f"{BASE_URL}{WEBHOOK_PATH}",
    }

    try:
        r = requests.post(MP_PAYMENTS_URL, json=payload, headers=mp_headers(), timeout=30)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Falha ao conectar no Mercado Pago: {str(e)}")

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Erro Mercado Pago: {r.text}")

    data = r.json()

    payment_id = str(data.get("id", ""))
    status = data.get("status", "unknown")

    # guarda no banco
    if payment_id:
        upsert_payment(payment_id, body.license_key, status)

    # pega info do QR / copia e cola
    poi = (data.get("point_of_interaction") or {})
    tx = (poi.get("transaction_data") or {})

    qr_code = tx.get("qr_code")  # copia e cola
    qr_code_base64 = tx.get("qr_code_base64")  # imagem base64
    ticket_url = tx.get("ticket_url")  # link (muito útil)

    return {
        "ok": True,
        "license_key": body.license_key,
        "amount": body.amount,
        "payment_id": payment_id,
        "status": status,
        "qr_code": qr_code,
        "qr_code_base64": qr_code_base64,
        "ticket_url": ticket_url,
        "notification_url": payload["notification_url"],
    }


@app.post("/webhook/mercadopago")
async def mercadopago_webhook(request: Request):
    """
    O MP pode enviar:
    - querystring (ex: ?type=payment&data.id=123)
    - ou body JSON
    Vamos tentar capturar o payment_id de qualquer jeito.
    """
    qs = dict(request.query_params)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    payment_id = None

    # 1) formato comum no querystring
    if "data.id" in qs:
        payment_id = qs.get("data.id")

    # 2) alguns casos vêm como id direto
    if not payment_id and "id" in qs:
        payment_id = qs.get("id")

    # 3) formato JSON
    if not payment_id and isinstance(body, dict):
        if isinstance(body.get("data"), dict) and body["data"].get("id"):
            payment_id = body["data"]["id"]
        elif body.get("id"):
            payment_id = body["id"]

    if not payment_id:
        # Mesmo sem id, devolve 200 para o MP não ficar tentando eternamente
        return JSONResponse({"ok": True, "ignored": True, "reason": "no_payment_id"}, status_code=200)

    # Consulta pagamento no MP para pegar status e external_reference (license_key)
    data = mp_get_payment(str(payment_id))
    status = data.get("status", "unknown")
    license_key = data.get("external_reference")  # nós mandamos isso como license_key

    if license_key:
        upsert_payment(str(payment_id), str(license_key), status)

    # Se aprovado: ativa a licença e estende o prazo
    if status == "approved" and license_key:
        paid_until_dt = update_license_paid(str(license_key))
        return JSONResponse(
            {"ok": True, "payment_id": str(payment_id), "status": status, "license_key": license_key, "paid_until": iso(paid_until_dt)},
            status_code=200,
        )

    return JSONResponse({"ok": True, "payment_id": str(payment_id), "status": status, "license_key": license_key}, status_code=200)


if __name__ == "__main__":
    import uvicorn

    # local: usa PORT se existir (Render também usa PORT)
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
