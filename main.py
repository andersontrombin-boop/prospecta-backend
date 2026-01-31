import os
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# =========================
# ENV / CONFIG
# =========================
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip()  # ex: https://prospecta-backend-u79p.onrender.com
DB_PATH = os.getenv("DB_PATH", "db.sqlite3").strip()
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))

if not BASE_URL:
    # fallback seguro para rodar local sem quebrar
    BASE_URL = "http://localhost:8000"

MP_PAYMENTS_URL = "https://api.mercadopago.com/v1/payments"

# Email padrão (válido) para evitar erro "payer.email must be a valid email"
DEFAULT_PAYER_EMAIL = os.getenv("DEFAULT_PAYER_EMAIL", "cliente@prospecta.app").strip()
if "@" not in DEFAULT_PAYER_EMAIL or "." not in DEFAULT_PAYER_EMAIL:
    DEFAULT_PAYER_EMAIL = "cliente@prospecta.app"


# =========================
# APP
# =========================
app = FastAPI(title="Prospecta Assinaturas", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ajuste se quiser travar
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# DB
# =========================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS licenses (
        license_key TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        paid_until TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        payment_id TEXT PRIMARY KEY,
        license_key TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        ticket_url TEXT,
        qr_code TEXT,
        qr_code_base64 TEXT,
        raw_json TEXT,
        FOREIGN KEY (license_key) REFERENCES licenses(license_key)
    )
    """)

    conn.commit()
    conn.close()

db_init()

# =========================
# HELPERS
# =========================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_iso(dt: Optional[str]) -> Optional[datetime]:
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except Exception:
        return None

def add_days_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

def ensure_license_exists(license_key: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT license_key FROM licenses WHERE license_key = ?", (license_key,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO licenses (license_key, created_at, paid_until) VALUES (?, ?, ?)",
            (license_key, now_iso(), None),
        )
        conn.commit()
    conn.close()

def set_license_paid(license_key: str, days: int = DEFAULT_BILLING_DAYS):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE licenses SET paid_until = ? WHERE license_key = ?", (add_days_iso(days), license_key))
    conn.commit()
    conn.close()

def get_license(license_key: str) -> Optional[Dict[str, Any]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def update_payment(payment_id: str, **fields):
    conn = db_connect()
    cur = conn.cursor()
    sets = []
    values = []
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        values.append(v)
    sets.append("updated_at = ?")
    values.append(now_iso())
    values.append(payment_id)
    cur.execute(f"UPDATE payments SET {', '.join(sets)} WHERE payment_id = ?", values)
    conn.commit()
    conn.close()

def insert_payment(
    payment_id: str,
    license_key: str,
    amount: float,
    status: str,
    ticket_url: Optional[str],
    qr_code: Optional[str],
    qr_code_base64: Optional[str],
    raw_json: Optional[str],
):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO payments
    (payment_id, license_key, amount, status, created_at, updated_at, ticket_url, qr_code, qr_code_base64, raw_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(payment_id),
        license_key,
        float(amount),
        status,
        now_iso(),
        now_iso(),
        ticket_url,
        qr_code,
        qr_code_base64,
        raw_json,
    ))
    conn.commit()
    conn.close()

def mp_headers(idempotency_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": idempotency_key,
    }

def mp_get_payment(payment_id: str) -> Dict[str, Any]:
    url = f"{MP_PAYMENTS_URL}/{payment_id}"
    r = requests.get(url, headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}, timeout=30)
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar pagamento no MP: {r.status_code} - {r.text}")
    return r.json()

# =========================
# SCHEMAS
# =========================
class LicenseCreate(BaseModel):
    api_key: str = Field(..., description="Chave ADMIN_API_KEY")
    license_key: str = Field(..., description="Licença a ser criada")

class PixCreate(BaseModel):
    license_key: str
    amount: float
    payer_email: Optional[str] = Field(
        default=None,
        description="Opcional. Se não enviar, o sistema usa um e-mail padrão válido."
    )

# =========================
# ROUTES
# =========================
@app.get("/health")
def health():
    return {"ok": True}

@app.post("/admin/create-license")
def create_license(body: LicenseCreate):
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY não configurada no Render (Environment).")

    if body.api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    ensure_license_exists(body.license_key)
    return {"ok": True, "license_key": body.license_key}

@app.get("/license/validate")
def validate_license(key: str):
    lic = get_license(key)
    if not lic:
        return {"valid": False}

    paid_until = parse_iso(lic.get("paid_until"))
    if paid_until and datetime.now(timezone.utc) <= paid_until:
        return {"valid": True, "paid_until": lic.get("paid_until")}

    return {"valid": False}

@app.post("/pix/create")
def create_pix(body: PixCreate):
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN não configurado no Render (Environment).")

    ensure_license_exists(body.license_key)

    # ✅ CORREÇÃO PRINCIPAL: garantir um e-mail válido sempre
    payer_email = (body.payer_email or DEFAULT_PAYER_EMAIL).strip()
    if "@" not in payer_email or "." not in payer_email:
        payer_email = DEFAULT_PAYER_EMAIL

    idempotency_key = str(uuid.uuid4())

    payload = {
        "transaction_amount": float(body.amount),
        "description": f"Licença {body.license_key}",
        "payment_method_id": "pix",
        "payer": {
            "email": payer_email
        },
        "notification_url": f"{BASE_URL.rstrip('/')}/mp/webhook",
        "external_reference": body.license_key,
    }

    try:
        r = requests.post(
            MP_PAYMENTS_URL,
            headers=mp_headers(idempotency_key),
            json=payload,
            timeout=30
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Falha ao chamar Mercado Pago: {str(e)}")

    if r.status_code not in (200, 201):
        # repassa erro do MP para você ver no Swagger
        raise HTTPException(status_code=500, detail=r.text)

    data = r.json()

    payment_id = str(data.get("id"))
    status = data.get("status", "unknown")

    poi = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    ticket_url = poi.get("ticket_url")
    qr_code = poi.get("qr_code")
    qr_code_base64 = poi.get("qr_code_base64")

    insert_payment(
        payment_id=payment_id,
        license_key=body.license_key,
        amount=float(body.amount),
        status=status,
        ticket_url=ticket_url,
        qr_code=qr_code,
        qr_code_base64=qr_code_base64,
        raw_json=json.dumps(data, ensure_ascii=False),
    )

    return {
        "payment_id": payment_id,
        "status": status,
        "ticket_url": ticket_url,
        "qr_code": qr_code,
        "qr_code_base64": qr_code_base64,
    }

@app.post("/mp/webhook")
async def mp_webhook(request: Request):
    # MP manda query: ?data.id=XXXXX&type=payment
    q = dict(request.query_params)
    payment_id = q.get("data.id") or q.get("id")  # tolerância
    event_type = q.get("type")

    # Também pode mandar JSON no body (depende configuração)
    try:
        body = await request.json()
    except Exception:
        body = {}

    if not payment_id:
        # nada pra processar
        return {"ok": True}

    # Só processa pagamentos
    if event_type and event_type != "payment":
        return {"ok": True}

    # Busca status real no MP
    mp_data = mp_get_payment(payment_id)
    status = mp_data.get("status", "unknown")

    external_ref = mp_data.get("external_reference") or ""
    license_key = external_ref.strip()

    poi = (mp_data.get("point_of_interaction") or {}).get("transaction_data") or {}
    ticket_url = poi.get("ticket_url")
    qr_code = poi.get("qr_code")
    qr_code_base64 = poi.get("qr_code_base64")

    # Atualiza payment no banco (se existir)
    # Se não existir, salva mesmo assim
    insert_payment(
        payment_id=str(payment_id),
        license_key=license_key or "UNKNOWN",
        amount=float(mp_data.get("transaction_amount") or 0),
        status=status,
        ticket_url=ticket_url,
        qr_code=qr_code,
        qr_code_base64=qr_code_base64,
        raw_json=json.dumps(mp_data, ensure_ascii=False),
    )

    # Se aprovado, ativa licença
    if status == "approved" and license_key:
        ensure_license_exists(license_key)
        set_license_paid(license_key, DEFAULT_BILLING_DAYS)

    return {"ok": True, "payment_id": str(payment_id), "status": status}
