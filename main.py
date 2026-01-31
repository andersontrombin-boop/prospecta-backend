import os
import json
import uuid
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, Dict

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# =========================
# CONFIG / ENV
# =========================
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()

BASE_URL = os.getenv("BASE_URL", "").strip()  # opcional
DB_PATH = os.getenv("DB_PATH", "app.db").strip()
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))

# email padrão (Mercado Pago normalmente exige payer.email)
DEFAULT_PAYER_EMAIL = os.getenv("DEFAULT_PAYER_EMAIL", "comprador@prospecta.local").strip()


# =========================
# HELPERS
# =========================
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init() -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            license_key TEXT PRIMARY KEY,
            paid_until TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT,
            last_payment_id TEXT,
            last_payment_status TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_license_row(license_key: str) -> Optional[sqlite3.Row]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,))
    row = cur.fetchone()
    conn.close()
    return row

def upsert_license(license_key: str, paid_until: datetime, status: str,
                   last_payment_id: Optional[str] = None,
                   last_payment_status: Optional[str] = None) -> None:
    now = iso(utc_now())
    conn = db_connect()
    cur = conn.cursor()

    existing = get_license_row(license_key)
    if existing is None:
        cur.execute("""
            INSERT INTO licenses (license_key, paid_until, status, created_at, updated_at, last_payment_id, last_payment_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            license_key,
            iso(paid_until),
            status,
            now,
            now,
            last_payment_id,
            last_payment_status
        ))
    else:
        cur.execute("""
            UPDATE licenses
            SET paid_until = ?, status = ?, updated_at = ?, last_payment_id = ?, last_payment_status = ?
            WHERE license_key = ?
        """, (
            iso(paid_until),
            status,
            now,
            last_payment_id,
            last_payment_status,
            license_key
        ))

    conn.commit()
    conn.close()

def extend_license_from_now_or_current(license_key: str, extra_days: int) -> datetime:
    row = get_license_row(license_key)
    now = utc_now()

    if row and row["paid_until"]:
        try:
            current = parse_iso(row["paid_until"])
        except Exception:
            current = now
    else:
        current = now

    base = current if current > now else now
    return base + timedelta(days=extra_days)

def require_admin_key_from_body(api_key: str) -> None:
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY não configurada no Render")
    if api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")


# =========================
# SCHEMAS
# =========================
class LicenseCreate(BaseModel):
    api_key: str = Field(..., description="Chave admin (ADMIN_API_KEY)")
    license_key: str = Field(..., min_length=3, description="Chave da licença")

class PixCreate(BaseModel):
    license_key: str = Field(..., min_length=3)
    amount: float = Field(..., gt=0)
    payer_email: Optional[str] = Field(None, description="Email do pagador (opcional)")

class LicenseValidateResponse(BaseModel):
    valid: bool
    paid_until: Optional[str] = None
    status: Optional[str] = None


# =========================
# APP
# =========================
app = FastAPI(title="Prospecta Assinaturas", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db_init()


# =========================
# ROUTES
# =========================
@app.get("/health")
def health():
    return {"ok": True, "time": iso(utc_now())}


@app.post("/admin/create-license")
def create_license(body: LicenseCreate):
    """
    Cria (ou recria) uma licença e define paid_until = agora + DEFAULT_BILLING_DAYS.
    Você chamou no Swagger com { api_key, license_key } e deu 200: ok.
    """
    require_admin_key_from_body(body.api_key)

    paid_until = utc_now() + timedelta(days=DEFAULT_BILLING_DAYS)
    upsert_license(
        license_key=body.license_key,
        paid_until=paid_until,
        status="active"
    )
    return {"ok": True, "license_key": body.license_key, "paid_until": iso(paid_until)}


@app.get("/license/validate", response_model=LicenseValidateResponse)
def validate_license(key: str):
    row = get_license_row(key)
    if not row:
        return {"valid": False}

    paid_until_str = row["paid_until"]
    status = row["status"] or "inactive"

    if not paid_until_str:
        return {"valid": False, "paid_until": None, "status": status}

    try:
        paid_until = parse_iso(paid_until_str)
    except Exception:
        return {"valid": False, "paid_until": paid_until_str, "status": status}

    valid = (status == "active") and (utc_now() <= paid_until)
    return {"valid": valid, "paid_until": iso(paid_until), "status": status}


@app.post("/pix/create")
def create_pix(body: PixCreate):
    """
    Cria um pagamento PIX no Mercado Pago.
    IMPORTANTÍSSIMO: MP exige header X-Idempotency-Key.
    Também usamos external_reference = license_key para ligar o pagamento à licença.
    """
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN não configurado no Render")

    idempotency_key = str(uuid.uuid4())

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": idempotency_key,
    }

    payer_email = (body.payer_email or DEFAULT_PAYER_EMAIL).strip()

    payload = {
        "transaction_amount": float(body.amount),
        "description": f"Licença {body.license_key}",
        "payment_method_id": "pix",
        "payer": {"email": payer_email},
        "external_reference": body.license_key,  # <-- liga pagamento à licença
        "notification_url": f"{BASE_URL.rstrip('/')}/mp/webhook" if BASE_URL else None,
    }
    # remove campos None
    payload = {k: v for k, v in payload.items() if v is not None}

    resp = requests.post("https://api.mercadopago.com/v1/payments", headers=headers, json=payload, timeout=60)
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=resp.text)

    data = resp.json()

    # Retorna o que você precisa pra mostrar pro usuário (QR e link)
    point = data.get("point_of_interaction", {}) or {}
    tx = (point.get("transaction_data", {}) or {})
    return {
        "ok": True,
        "id": data.get("id"),
        "status": data.get("status"),
        "license_key": body.license_key,
        "qr_code": tx.get("qr_code"),
        "qr_code_base64": tx.get("qr_code_base64"),
        "ticket_url": tx.get("ticket_url"),
        "idempotency_key": idempotency_key,
    }


def _safe_json(body: bytes) -> Dict[str, Any]:
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}

def _mp_get_payment(payment_id: str) -> Dict[str, Any]:
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN não configurado no Render")
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    resp = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}", headers=headers, timeout=60)
    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail=resp.text)
    return resp.json()


# ✅ Duas rotas para evitar 404 por causa de barra final
@app.post("/mp/webhook")
@app.post("/mp/webhook/")
async def mp_webhook(request: Request):
    """
    Mercado Pago manda:
      POST /mp/webhook?data.id=XXXX&type=payment
    Muitas vezes o body é vazio.
    """
    qp = dict(request.query_params)
    body_bytes = await request.body()
    body_json = _safe_json(body_bytes)

    payment_id = (
        qp.get("data.id")
        or qp.get("id")
        or (body_json.get("data", {}) or {}).get("id")
        or body_json.get("id")
    )

    event_type = qp.get("type") or body_json.get("type")

    # Log básico pra você ver no Render
    print("MP WEBHOOK RECEBIDO:", {"query": qp, "type": event_type, "payment_id": payment_id})

    if not payment_id:
        # responde 200 mesmo assim, pra MP não ficar reenviando infinitamente
        return {"ok": True, "ignored": True, "reason": "no_payment_id"}

    # Busca o pagamento no MP
    payment = _mp_get_payment(str(payment_id))
    status = payment.get("status")
    external_reference = payment.get("external_reference")  # deve ser license_key
    mp_payment_id = str(payment.get("id"))

    print("MP PAYMENT:", {"id": mp_payment_id, "status": status, "external_reference": external_reference})

    # Se não tiver external_reference, não dá pra ligar à licença
    if not external_reference:
        return {"ok": True, "ignored": True, "reason": "no_external_reference"}

    license_key = str(external_reference)

    # Atualiza status do pagamento na licença (mesmo que não aprovado)
    row = get_license_row(license_key)
    if row is None:
        # Se a licença não existir, cria como inactive (pra não perder o pagamento)
        upsert_license(
            license_key=license_key,
            paid_until=utc_now(),
            status="inactive",
            last_payment_id=mp_payment_id,
            last_payment_status=status,
        )
    else:
        # mantém paid_until atual e só atualiza campos
        try:
            current_paid = parse_iso(row["paid_until"]) if row["paid_until"] else utc_now()
        except Exception:
            current_paid = utc_now()

        upsert_license(
            license_key=license_key,
            paid_until=current_paid,
            status=row["status"] or "inactive",
            last_payment_id=mp_payment_id,
            last_payment_status=status,
        )

    # Se aprovado, ativa/estende licença
    if status == "approved":
        new_paid_until = extend_license_from_now_or_current(license_key, DEFAULT_BILLING_DAYS)
        upsert_license(
            license_key=license_key,
            paid_until=new_paid_until,
            status="active",
            last_payment_id=mp_payment_id,
            last_payment_status=status,
        )
        print("LICENÇA ATUALIZADA:", {"license_key": license_key, "paid_until": iso(new_paid_until)})

    return {"ok": True}


# (Opcional) rota raiz para evitar 404 no GET /
@app.get("/")
def root():
    return {"ok": True, "service": "prospecta-backend", "docs": "/docs"}
