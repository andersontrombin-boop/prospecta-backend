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
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, Boolean
)
from sqlalchemy.orm import declarative_base, sessionmaker

# =========================
# CONFIG ENV
# =========================
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "SENHA_FORTE123")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")

# ✅ Postgres no Render (obrigatório agora)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))

# ✅ Trial (novo)
TRIAL_CODE = os.getenv("TRIAL_CODE", "TESTE48H")
TRIAL_HOURS = int(os.getenv("TRIAL_HOURS", "48"))

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não está configurada no Render (Environment).")

# =========================
# FASTAPI
# =========================
app = FastAPI(
    title="Prospecta Assinaturas",
    version="1.0.0"
)

# =========================
# DATABASE (SQLAlchemy)
# =========================
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def now_utc():
    return datetime.now(timezone.utc)


# =========================
# MODELS (Tabelas)
# =========================
class License(Base):
    __tablename__ = "licenses"

    id = Column(Integer, primary_key=True, index=True)
    license_key = Column(String, unique=True, index=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    active = Column(Boolean, default=True, nullable=False)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(String, index=True, nullable=False)
    license_key = Column(String, index=True, nullable=False)
    status = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class TrialDevice(Base):
    __tablename__ = "trial_devices"

    id = Column(Integer, primary_key=True, index=True)
    machine_id = Column(Text, unique=True, index=True, nullable=False)

    first_activated_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)


# cria as tabelas se não existirem
Base.metadata.create_all(bind=engine)


# =========================
# SCHEMAS (Pydantic)
# =========================
class LicenseCreate(BaseModel):
    api_key: str
    license_key: str


class PixCreate(BaseModel):
    license_key: str
    amount: float
    payer_email: str  # string simples


class TrialActivateRequest(BaseModel):
    code: str = Field(..., description="Código padrão do trial, ex: TESTE48H")
    machine_id: str = Field(..., min_length=6, max_length=200, description="ID único do PC (hash)")


class LicenseStatusResponse(BaseModel):
    valid: bool
    license_key: Optional[str] = None
    expires_at: Optional[str] = None
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

    expires_at = now_utc() + timedelta(days=DEFAULT_BILLING_DAYS)

    db = SessionLocal()
    try:
        exists = db.query(License).filter(License.license_key == data.license_key).first()
        if exists:
            raise HTTPException(status_code=400, detail="Licença já existe")

        lic = License(
            license_key=data.license_key,
            expires_at=expires_at,
            active=True
        )
        db.add(lic)
        db.commit()

        return {
            "ok": True,
            "license_key": data.license_key,
            "expires_at": expires_at.isoformat()
        }
    finally:
        db.close()


# =========================
# VALIDATE LICENSE (PAGA)
# =========================
@app.get("/license/validate")
def validate_license(key: str):
    db = SessionLocal()
    try:
        lic = db.query(License).filter(License.license_key == key, License.active == True).first()
        if not lic:
            return {"valid": False}

        if lic.expires_at <= now_utc():
            return {"valid": False}

        return {
            "valid": True,
            "license_key": key,
            "expires_at": lic.expires_at.isoformat()
        }
    finally:
        db.close()


# =========================
# TRIAL 48H (NOVO) - trava por PC
# =========================
@app.post("/license/activate-trial", response_model=LicenseStatusResponse)
def activate_trial(payload: TrialActivateRequest):
    if payload.code.strip().upper() != TRIAL_CODE.strip().upper():
        raise HTTPException(status_code=401, detail="Código de teste inválido.")

    machine_id = payload.machine_id.strip()
    if not machine_id:
        raise HTTPException(status_code=422, detail="machine_id é obrigatório")

    db = SessionLocal()
    try:
        dev = db.query(TrialDevice).filter(TrialDevice.machine_id == machine_id).first()
        now = now_utc()

        # primeira vez: cria trial e expira em 48h
        if dev is None:
            exp = now + timedelta(hours=TRIAL_HOURS)
            dev = TrialDevice(
                machine_id=machine_id,
                first_activated_at=now,
                expires_at=exp,
                last_seen_at=now
            )
            db.add(dev)
            db.commit()
            return LicenseStatusResponse(valid=True, license_key=TRIAL_CODE, expires_at=exp.isoformat())

        # já existe: atualiza last_seen
        dev.last_seen_at = now
        db.commit()

        # ainda válido
        if dev.expires_at > now:
            return LicenseStatusResponse(valid=True, license_key=TRIAL_CODE, expires_at=dev.expires_at.isoformat())

        # expirou: bloqueia para sempre nesse PC
        return LicenseStatusResponse(valid=False, reason="Trial já utilizado neste dispositivo.")

    finally:
        db.close()


# =========================
# CREATE PIX
# =========================
@app.post("/pix/create")
def create_pix(data: PixCreate):
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Mercado Pago não configurado")

    db = SessionLocal()
    try:
        lic = db.query(License).filter(License.license_key == data.license_key, License.active == True).first()
        if not lic:
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

        p = Payment(
            payment_id=str(payment["id"]),
            license_key=data.license_key,
            status=str(payment.get("status", "")),
            created_at=now_utc()
        )
        db.add(p)
        db.commit()

        tx = payment["point_of_interaction"]["transaction_data"]
        return {
            "payment_id": payment["id"],
            "status": payment["status"],
            "qr_code": tx.get("qr_code"),
            "qr_code_base64": tx.get("qr_code_base64"),
            "ticket_url": tx.get("ticket_url")
        }
    finally:
        db.close()


# =========================
# MERCADO PAGO WEBHOOK
# =========================
@app.post("/mp/webhook")
async def mp_webhook(request: Request):
    if not MP_ACCESS_TOKEN:
        return {"ok": True}

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

    db = SessionLocal()
    try:
        # atualiza payments
        p = db.query(Payment).filter(Payment.payment_id == str(payment_id)).first()
        if p:
            p.status = str(status)

        # se aprovado, estende licença
        if status == "approved":
            # acha qual license_key está no payment
            lic_key = p.license_key if p else None
            if lic_key:
                lic = db.query(License).filter(License.license_key == lic_key).first()
                if lic:
                    lic.expires_at = now_utc() + timedelta(days=DEFAULT_BILLING_DAYS)
                    lic.active = True

        db.commit()
        return {"ok": True}
    finally:
        db.close()


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
