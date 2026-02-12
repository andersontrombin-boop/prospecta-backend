from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
import os
import psycopg2
import psycopg2.extras

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

# ===============================
# MODELO
# ===============================

class ActivateRequest(BaseModel):
    license_key: str
    device_id: str

# ===============================
# HEALTH
# ===============================

@app.get("/health")
def health():
    try:
        conn = get_conn()
        conn.close()
        return {"ok": True, "db": "connected"}
    except:
        return {"ok": False, "db": "error"}

# ===============================
# ATIVAR LICENÇA
# ===============================

@app.post("/activate")
def activate(data: ActivateRequest):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # busca licença
    cur.execute("SELECT * FROM licenses WHERE license_key = %s", (data.license_key,))
    license = cur.fetchone()

    if not license:
        raise HTTPException(status_code=404, detail="Licença não encontrada")

    if license["status"] != "active":
        raise HTTPException(status_code=403, detail="Licença inativa")

    license_id = license["id"]
    license_type = license["license_type"]

    # verifica se já existe ativação
    cur.execute("""
        SELECT * FROM license_activations
        WHERE license_id = %s AND device_id = %s
    """, (license_id, data.device_id))

    existing = cur.fetchone()

    if existing:
        # verifica se expirou
        if existing["expires_at"] < datetime.utcnow():
            raise HTTPException(status_code=403, detail="Licença expirada")

        return {
            "status": "ok",
            "expires_at": existing["expires_at"]
        }

    # NOVA ATIVAÇÃO
    if license_type == "trial":
        expires_at = datetime.utcnow() + timedelta(hours=48)
    else:
        expires_at = datetime.utcnow() + timedelta(days=30)

    try:
        cur.execute("""
            INSERT INTO license_activations
            (license_id, device_id, license_type, expires_at)
            VALUES (%s, %s, %s, %s)
        """, (license_id, data.device_id, license_type, expires_at))
        conn.commit()
    except:
        raise HTTPException(status_code=403, detail="Essa licença já foi usada neste dispositivo")

    return {
        "status": "activated",
        "expires_at": expires_at
    }
