import os
import traceback

import psycopg
from fastapi import FastAPI

app = FastAPI()


def get_conn():
    db_url = os.getenv("DATABASE_URL")

    if not db_url:
        raise RuntimeError("DATABASE_URL não definido no Render (Environment).")

    # garante sslmode=require
    if "sslmode=" not in db_url:
        if "?" in db_url:
            db_url = db_url + "&sslmode=require"
        else:
            db_url = db_url + "?sslmode=require"

    # conexão
    return psycopg.connect(db_url, connect_timeout=8)


@app.get("/health")
def health():
    try:
        conn = get_conn()
        conn.close()
        return {"ok": True, "db": "connected"}
    except Exception as e:
        return {"ok": False, "db": "error", "detail": str(e)}


@app.get("/")
def root():
    return {"ok": True, "service": "prospecta-backend"}
