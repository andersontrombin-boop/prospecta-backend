import os
from fastapi import FastAPI
from sqlalchemy import create_engine, text

app = FastAPI(title="Prospecta Backend", version="1.0.0")


def get_database_url() -> str:
    """
    Render: usa DATABASE_URL nas env vars.
    Importante: forçar o driver psycopg (v3) para evitar psycopg2.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL não definido nas variáveis do Render.")

    # Força SQLAlchemy a usar psycopg v3 (não psycopg2)
    # Aceita tanto postgresql:// quanto postgres://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    if url.startswith("postgresql://") and "postgresql+psycopg://" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    # Garante SSL para Supabase (quase sempre necessário)
    if "sslmode=" not in url:
        joiner = "&" if "?" in url else "?"
        url = url + f"{joiner}sslmode=require"

    return url


DATABASE_URL = get_database_url()

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=180,
)


@app.get("/")
def root():
    return {"ok": True, "service": "prospecta-backend"}


@app.get("/health")
def health():
    # Teste real de conexão com o banco
    with engine.connect() as conn:
        conn.execute(text("select 1"))
    return {"ok": True, "db": "connected"}
