import os
import psycopg2
import traceback
from fastapi import FastAPI

app = FastAPI()


# =========================
# CONEXÃO COM BANCO
# =========================
def get_conn():
    db_url = os.getenv("DATABASE_URL")

    if not db_url:
        raise RuntimeError("DATABASE_URL não definido no Render")

    # garante sslmode=require
    if "sslmode=" not in db_url:
        sep = "&" if "?" in db_url else "?"
        db_url = db_url + f"{sep}sslmode=require"

    return psycopg2.connect(db_url)


# =========================
# HEALTH CHECK
# =========================
@app.get("/health")
def health():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        cur.fetchone()
        cur.close()
        conn.close()
        return {"ok": True, "db": "connected"}
    except Exception as e:
        traceback.print_exc()
        return {
            "ok": False,
            "db": "error",
            "detail": str(e)
        }
