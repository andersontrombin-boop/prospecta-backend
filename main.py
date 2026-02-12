import os
import traceback
import psycopg
from fastapi import FastAPI

app = FastAPI()


def get_conn():
    db_url = os.getenv("DATABASE_URL")

    if not db_url:
        raise RuntimeError("DATABASE_URL não definido")

    if "sslmode=" not in db_url:
        sep = "&" if "?" in db_url else "?"
        db_url = db_url + f"{sep}sslmode=require"

    return psycopg.connect(db_url)


@app.get("/health")
def health():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()

        return {"ok": True, "db": "connected"}

    except Exception as e:
        traceback.print_exc()
        return {
            "ok": False,
            "db": "error",
            "detail": str(e)
        }
