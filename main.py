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
# CONFIG ENV (SEM FALLBACK)
# =========================
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
DB_PATH = os.getenv("DB_PATH", "app.db")
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))

# Falha rápida e segura: se não tiver ADMIN_API_KEY, não sobe
if not ADMIN_API_KEY or len(ADMIN_API_KEY.strip()) < 8:
    raise RuntimeError("ADMIN_API_KEY não configurada (ou muito curta). Configure no Render > Environment.")

# =========================
# FASTAPI
# =========================
app = FastAPI(
    title="Prospecta Assinaturas",
    version="1.2.1"
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

    # Migrações (não quebram banco antigo)
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

    # >>> TRAVA POR PC / ATIVAÇÃO ÚNICA <<<
    if not table_has_column(conn, "licenses", "device_id"):
        cur.execute("ALTER TABLE licenses ADD COLUMN device_id TEXT")  # PC vinculado
    if not table_has_column(conn, "licenses", "activated_at"):
        cur.execute("ALTER TABLE licenses ADD COLUMN activated_at TEXT")  # quando vinculou

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

def parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None

def compute_expiration(plan: Literal["trial", "monthly"]) -> datetime:
    if plan == "trial":
        return utcnow() + timedelta(hours=48)
    return utcnow() + timedelta(days=DEFAULT_BILLING_DAYS)

def gen_license_key() -> str:
    return secrets.token_urlsafe(18).replace("-", "").replace("_", "")

def is_active_row(row: sqlite3.Row) -> bool:
    # Ativa = active=1, não revogada, e não expirada
    if int(row["active"] or 0) != 1:
        return False
    revoked = row["revoked"]
    if revoked is not None and int(revoked) == 1:
        return False
    exp = parse_iso(row["expires_at"])
    if exp is None:
        return False
    return exp >= utcnow()

def serialize_license(row: sqlite3.Row) -> dict:
    exp = row["expires_at"]
    issued = row["issued_at"]
    activated = row["activated_at"]
    return {
        "id": row["id"],
        "license_key": row["license_key"],
        "plan": row["plan"],
        "issued_at": issued,
        "expires_at": exp,
        "active_flag": int(row["active"] or 0),
        "revoked": int(row["revoked"] or 0),
        "revoked_at": row["revoked_at"],
        "revoke_reason": row["revoke_reason"],
        "device_id": row["device_id"],
        "activated_at": activated,
        "status": (
            "revoked" if (row["revoked"] is not None and int(row["revoked"]) == 1)
            else "active" if is_active_row(row)
            else "expired" if (parse_iso(row["expires_at"]) and parse_iso(row["expires_at"]) < utcnow())
            else "inactive"
        )
    }

# =========================
# MODELS
# =========================
PlanType = Literal["trial", "monthly"]
LicenseStatus = Literal["active", "expired", "trial", "revoked", "inactive", "all"]

class LicenseCreate(BaseModel):
    api_key: str
    plan: PlanType  # trial=48h | monthly=30d
    license_key: Optional[str] = None

class PixCreate(BaseModel):
    license_key: str
    amount: float
    payer_email: str

class AdminResetDevice(BaseModel):
    api_key: str
    license_key: str
    reason: Optional[str] = None

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

    license_key = (data.license_key or "").strip()
    if not license_key:
        license_key = gen_license_key()

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO licenses (
              license_key, expires_at, active, plan, issued_at,
              revoked, revoked_at, revoke_reason,
              device_id, activated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                license_key,
                expires_at.isoformat(),
                1,
                plan,
                issued_at.isoformat(),
                0,
                None,
                None,
                None,   # ainda não ativou em nenhum PC
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
        "expires_at": expires_at.isoformat(),
        "device_id": None,
        "activated_at": None
    }

# =========================
# ✅ NEW: LIST LICENSES (ADMIN)
# =========================
@app.get("/admin/licenses")
def admin_list_licenses(
    api_key: str,
    status: LicenseStatus = "all",
    search: Optional[str] = None,
    limit: int = 200,
    offset: int = 0
):
    if api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    # limites seguros
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000
    if offset < 0:
        offset = 0

    s = (search or "").strip()
    like = f"%{s}%"

    conn = get_db()
    cur = conn.cursor()

    where = []
    params = []

    if s:
        # Busca por partes relevantes
        where.append("(license_key LIKE ? OR device_id LIKE ? OR plan LIKE ?)")
        params.extend([like, like, like])

    # Filtros por status
    now_iso = utcnow().isoformat()

    if status == "active":
        where.append("(active = 1 AND (revoked IS NULL OR revoked = 0) AND expires_at >= ?)")
        params.append(now_iso)

    elif status == "expired":
        where.append("(expires_at < ? AND (revoked IS NULL OR revoked = 0))")
        params.append(now_iso)

    elif status == "trial":
        # trial ainda válido
        where.append("(plan = 'trial' AND active = 1 AND (revoked IS NULL OR revoked = 0) AND expires_at >= ?)")
        params.append(now_iso)

    elif status == "revoked":
        where.append("(revoked = 1)")

    elif status == "inactive":
        # inactive = flag desligado OU sem expires_at válido (geralmente não acontece)
        where.append("(active != 1 OR active IS NULL)")

    # Monta query
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT *
        FROM licenses
        {where_sql}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    cur.execute(sql, tuple(params))
    rows = cur.fetchall()

    # total (para paginação)
    count_sql = f"SELECT COUNT(1) as total FROM licenses{where_sql}"
    cur.execute(count_sql, tuple(params[:-2]))  # sem limit/offset
    total = int(cur.fetchone()["total"])

    conn.close()

    items = [serialize_license(r) for r in rows]

    return {
        "ok": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items
    }

# =========================
# VALIDATE LICENSE (COM TRAVA POR PC)
# Agora exige device_id.
#
# Fluxo:
# 1) Se licença ainda NÃO tem device_id -> vincula no primeiro validate
# 2) Se já tem device_id -> só valida se bater exatamente
# =========================
@app.get("/license/validate")
def validate_license(key: str, device_id: str):
    key = (key or "").strip()
    device_id = (device_id or "").strip()

    if not key:
        return {"valid": False, "reason": "missing_key"}
    if not device_id or len(device_id) < 8:
        return {"valid": False, "reason": "missing_device_id"}

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM licenses WHERE license_key = ?", (key,))
    row = cur.fetchone()

    if not row:
        conn.close()
        return {"valid": False, "reason": "not_found"}

    if int(row["active"]) != 1:
        conn.close()
        return {"valid": False, "reason": "inactive"}

    revoked = row["revoked"]
    if revoked is not None and int(revoked) == 1:
        conn.close()
        return {"valid": False, "reason": "revoked"}

    # Expiração
    exp = parse_iso(row["expires_at"])
    if exp is None or exp < utcnow():
        conn.close()
        return {"valid": False, "reason": "expired"}

    stored_device = (row["device_id"] or "").strip()

    # Primeira ativação: vincula device_id (ativação única)
    if not stored_device:
        cur.execute(
            "UPDATE licenses SET device_id = ?, activated_at = ? WHERE license_key = ?",
            (device_id, utcnow().isoformat(), key)
        )
        conn.commit()
        conn.close()
        return {
            "valid": True,
            "bound": True,  # vinculou agora
            "license_key": key,
            "plan": row["plan"],
            "issued_at": row["issued_at"],
            "expires_at": row["expires_at"]
        }

    # Já vinculada: só aceita no mesmo PC
    if stored_device != device_id:
        conn.close()
        return {"valid": False, "reason": "device_mismatch"}

    conn.close()
    return {
        "valid": True,
        "bound": False,
        "license_key": key,
        "plan": row["plan"],
        "issued_at": row["issued_at"],
        "expires_at": row["expires_at"]
    }

# =========================
# ADMIN: RESETAR VÍNCULO DE PC (quando cliente troca PC)
# =========================
@app.post("/admin/reset-device")
def admin_reset_device(data: AdminResetDevice):
    if data.api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    key = (data.license_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="license_key obrigatório")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM licenses WHERE license_key = ?", (key,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Licença não encontrada")

    cur.execute(
        "UPDATE licenses SET device_id = NULL, activated_at = NULL WHERE license_key = ?",
        (key,)
    )
    conn.commit()
    conn.close()

    return {"ok": True, "license_key": key, "reset": True, "reason": data.reason or "admin_reset"}

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
        "payer": {"email": data.payer_email}
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
        (str(payment["id"]), data.license_key, payment["status"], utcnow().isoformat())
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
    _payload = await request.json()
    payment_id = request.query_params.get("data.id")

    if not payment_id:
        return {"ok": True}

    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}

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
            ((utcnow() + timedelta(days=DEFAULT_BILLING_DAYS)).isoformat(), str(payment_id))
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
