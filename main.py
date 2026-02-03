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
    machine_id: str = Field(..., min_length=6, max_lengt_
