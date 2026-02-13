import os
import secrets
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal, Any, Dict, List

import psycopg
from psycopg.errors import UniqueViolation
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware  # ✅ ADD
from pydantic import BaseModel, Field

# =========================
# CONFIG
# =========================
ADMIN_API_KEY = (os.getenv("ADMIN_API_KEY") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

if not ADMIN_API_KEY or len(ADMIN_API_KEY) < 8:
    raise RuntimeError("ADMIN_API_KEY não configurada no Render > Environment.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não configurada no Render > Environment.")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connect_db():
    return psycopg.connect(DATABASE_URL, connect_timeout=8)


def fetchone_dict(cur) -> Optional[Dict[str, Any]]:
    row = cur.fetchone()
    if not row:
        return None
    cols = [d.name for d in cur.description]
    return dict(zip(cols, row))


def fetchall_dict(cur) -> List[Dict[str, Any]]:
    rows = cur.fetchall() or []
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def gen_license_key() -> str:
    return secrets.token_urlsafe(18).replace("-", "").replace("_", "")


LicenseType = Literal["trial", "monthly"]
LicenseStatus = Literal["active", "blocked", "expired", "canceled"]

app = FastAPI(title="Prospecta Backend", version="2.0.2")

# =========================
# ✅ CORS (para Painel Admin local)
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# MODELS
# =========================
class AdminCreateLicense(BaseModel):
    api_key: str
    license_type: LicenseType = "trial"
    duration_hours: Optional[int] = None  # trial=48, monthly=720
    license_key: Optional[str] = None
    status: LicenseStatus = "active"
    buyer_email: Optional[str] = None  # opcional


class AdminResetLicense(BaseModel):
    api_key: str
    license_key: str


class AdminRevokeLicense(BaseModel):
    api_key: str
    license_key: str
    status: LicenseStatus = "blocked"


class ActivateRequest(BaseModel):
    license_key: str = Field(..., min_length=3)
    device_id: str = Field(..., min_length=6)
    buyer_email: Optional[str] = None  # obrigatório só no monthly


# =========================
# HEALTH
# =========================
@app.get("/health")
def health():
    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
        return {"ok": True, "db": "connected"}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "db": "error", "detail": str(e)}


@app.get("/")
def root():
    return {"ok": True, "service": "prospecta-backend"}


# =========================
# ADMIN: CREATE LICENSE
# =========================
@app.post("/admin/create-license")
def admin_create_license(data: AdminCreateLicense):
    if data.api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    key = (data.license_key or "").strip()
    if not key:
        key = gen_license_key()

    ltype: LicenseType = data.license_type
    status: LicenseStatus = data.status
    hours = int(data.duration_hours) if (data.duration_hours and data.duration_hours > 0) else (48 if ltype == "trial" else 720)
    created_at = utcnow()

    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.licenses
                      (license_key, license_type, status, duration_hours, created_at, activated_at, expires_at, device_id, buyer_email)
                    VALUES
                      (%s, %s, %s, %s, %s, NULL, NULL, NULL, %s)
                    RETURNING id, license_key, license_type, status, duration_hours, created_at;
                    """,
                    (key, ltype, status, hours, created_at, (data.buyer_email or None)),
                )
                row = fetchone_dict(cur)
        return {"ok": True, "license": row}

    except UniqueViolation:
        raise HTTPException(status_code=400, detail="Licença já existe (license_key duplicada)")
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao criar licença: {str(e)}")


# =========================
# ADMIN: LIST LICENSES
# =========================
@app.get("/admin/licenses")
def admin_list_licenses(api_key: str, search: Optional[str] = None, limit: int = 200, offset: int = 0):
    if api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    s = (search or "").strip()

    where_sql = ""
    params: List[Any] = []
    if s:
        where_sql = "WHERE license_key ILIKE %s OR buyer_email ILIKE %s"
        like = f"%{s}%"
        params.extend([like, like])

    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, license_key, license_type, status, duration_hours, created_at, activated_at, expires_at, buyer_email
                FROM public.licenses
                {where_sql}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s;
                """,
                (*params, limit, offset),
            )
            items = fetchall_dict(cur)
            cur.execute(f"SELECT COUNT(1) FROM public.licenses {where_sql};", tuple(params))
            total = cur.fetchone()[0]

    return {"ok": True, "total": total, "limit": limit, "offset": offset, "items": items}


# =========================
# ADMIN: REVOKE LICENSE
# =========================
@app.post("/admin/revoke-license")
def admin_revoke_license(data: AdminRevokeLicense):
    if data.api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    key = data.license_key.strip()

    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.licenses SET status = %s WHERE license_key = %s RETURNING license_key, status;",
                (data.status, key),
            )
            row = fetchone_dict(cur)
            if not row:
                raise HTTPException(status_code=404, detail="Licença não encontrada")

    return {"ok": True, "license": row}


# =========================
# ADMIN: RESET LICENSE (apaga ativações)
# =========================
@app.post("/admin/reset-license")
def admin_reset_license(data: AdminResetLicense):
    if data.api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    key = data.license_key.strip()

    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM public.licenses WHERE license_key = %s;", (key,))
            lic = fetchone_dict(cur)
            if not lic:
                raise HTTPException(status_code=404, detail="Licença não encontrada")

            license_id = lic["id"]
            cur.execute("DELETE FROM public.license_activations WHERE license_id = %s;", (license_id,))
            cur.execute("UPDATE public.licenses SET activated_at = NULL, expires_at = NULL, device_id = NULL WHERE id = %s;", (license_id,))

    return {"ok": True, "license_key": key, "reset": True}


# =========================
# LICENSE: ACTIVATE / VALIDATE
# TRIAL (chave global): cada PC tem 48h a partir da 1ª ativação naquele PC
# MONTHLY: trava por licença + PC e exige buyer_email
# =========================
@app.post("/license/activate")
def activate_license(data: ActivateRequest):
    key = data.license_key.strip()
    device_id = data.device_id.strip()
    buyer_email = (data.buyer_email or "").strip().lower() or None

    if not key:
        raise HTTPException(status_code=400, detail="license_key obrigatório")
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id obrigatório")

    now = utcnow()

    with connect_db() as conn:
        with conn.cursor() as cur:
            # pega licença
            cur.execute(
                """
                SELECT id, license_key, license_type, status, duration_hours
                FROM public.licenses
                WHERE license_key = %s;
                """,
                (key,),
            )
            lic = fetchone_dict(cur)
            if not lic:
                raise HTTPException(status_code=404, detail="Licença não encontrada")

            if lic["status"] != "active":
                raise HTTPException(status_code=403, detail=f"Licença não ativa (status={lic['status']})")

            license_id = lic["id"]
            ltype = lic["license_type"]
            hours = int(lic.get("duration_hours") or (48 if ltype == "trial" else 720))

            # ========= TRIAL POR PC =========
            if ltype == "trial":
                # busca ativação desse PC para essa licença
                cur.execute(
                    """
                    SELECT activated_at, expires_at
                    FROM public.license_activations
                    WHERE license_id = %s AND device_id = %s AND license_type = 'trial'
                    ORDER BY activated_at DESC
                    LIMIT 1;
                    """,
                    (license_id, device_id),
                )
                act = fetchone_dict(cur)

                if act:
                    exp = act["expires_at"]
                    if exp and exp >= now:
                        return {
                            "ok": True,
                            "valid": True,
                            "already_activated": True,
                            "license_key": key,
                            "license_type": "trial",
                            "device_id": device_id,
                            "activated_at": act["activated_at"].isoformat() if act["activated_at"] else None,
                            "expires_at": exp.isoformat() if exp else None,
                        }
                    raise HTTPException(status_code=403, detail="Trial expirado neste PC (48h encerradas)")

                # primeira vez desse PC: cria ativação 48h
                activated_at = now
                expires_at = now + timedelta(hours=hours)

                try:
                    cur.execute(
                        """
                        INSERT INTO public.license_activations
                          (license_id, device_id, buyer_email, license_type, activated_at, expires_at)
                        VALUES
                          (%s, %s, NULL, 'trial', %s, %s)
                        RETURNING activated_at, expires_at;
                        """,
                        (license_id, device_id, activated_at, expires_at),
                    )
                    new_act = fetchone_dict(cur)

                    # opcional: marcar que a licença já foi usada por alguém (não bloqueia)
                    cur.execute(
                        """
                        UPDATE public.licenses
                        SET activated_at = COALESCE(activated_at, %s)
                        WHERE id = %s;
                        """,
                        (activated_at, license_id),
                    )

                except Exception:
                    traceback.print_exc()
                    raise HTTPException(status_code=403, detail="Ativação bloqueada (trial já ativado neste PC)")

                return {
                    "ok": True,
                    "valid": True,
                    "already_activated": False,
                    "license_key": key,
                    "license_type": "trial",
                    "device_id": device_id,
                    "activated_at": new_act["activated_at"].isoformat() if new_act else activated_at.isoformat(),
                    "expires_at": new_act["expires_at"].isoformat() if new_act else expires_at.isoformat(),
                }

            # ========= MONTHLY =========
            if not buyer_email:
                raise HTTPException(status_code=400, detail="buyer_email obrigatório para licença mensal")

            # se já ativou nesse PC, valida
            cur.execute(
                """
                SELECT activated_at, expires_at, buyer_email
                FROM public.license_activations
                WHERE license_id = %s AND device_id = %s AND license_type = 'monthly'
                ORDER BY activated_at DESC
                LIMIT 1;
                """,
                (license_id, device_id),
            )
            actm = fetchone_dict(cur)

            if actm:
                exp = actm["expires_at"]
                if exp and exp >= now:
                    if (actm.get("buyer_email") or "").lower() != buyer_email:
                        raise HTTPException(status_code=403, detail="Email não confere para esta licença")
                    return {
                        "ok": True,
                        "valid": True,
                        "already_activated": True,
                        "license_key": key,
                        "license_type": "monthly",
                        "device_id": device_id,
                        "activated_at": actm["activated_at"].isoformat() if actm["activated_at"] else None,
                        "expires_at": exp.isoformat() if exp else None,
                    }
                raise HTTPException(status_code=403, detail="Licença mensal expirada neste PC")

            # trava 1 PC por licença: se existe ativação em outro PC, bloqueia
            cur.execute(
                """
                SELECT device_id
                FROM public.license_activations
                WHERE license_id = %s AND license_type = 'monthly'
                ORDER BY activated_at DESC
                LIMIT 1;
                """,
                (license_id,),
            )
            other = fetchone_dict(cur)
            if other and other["device_id"] and other["device_id"] != device_id:
                raise HTTPException(status_code=403, detail="Licença mensal já ativada em outro PC (use reset no admin)")

            activated_at = now
            expires_at = now + timedelta(hours=hours)

            try:
                cur.execute(
                    """
                    INSERT INTO public.license_activations
                      (license_id, device_id, buyer_email, license_type, activated_at, expires_at)
                    VALUES
                      (%s, %s, %s, 'monthly', %s, %s)
                    RETURNING activated_at, expires_at;
                    """,
                    (license_id, device_id, buyer_email, activated_at, expires_at),
                )
                newm = fetchone_dict(cur)

                cur.execute(
                    """
                    UPDATE public.licenses
                    SET activated_at = COALESCE(activated_at, %s),
                        device_id = COALESCE(device_id, %s),
                        buyer_email = COALESCE(buyer_email, %s)
                    WHERE id = %s;
                    """,
                    (activated_at, device_id, buyer_email, license_id),
                )

            except Exception:
                traceback.print_exc()
                raise HTTPException(status_code=403, detail="Ativação bloqueada (licença travada / índice unique)")

            return {
                "ok": True,
                "valid": True,
                "already_activated": False,
                "license_key": key,
                "license_type": "monthly",
                "device_id": device_id,
                "activated_at": newm["activated_at"].isoformat() if newm else activated_at.isoformat(),
                "expires_at": newm["expires_at"].isoformat() if newm else expires_at.isoformat(),
            }


@app.get("/license/validate")
def validate_license(key: str, device_id: str, buyer_email: Optional[str] = None):
    data = ActivateRequest(license_key=key, device_id=device_id, buyer_email=buyer_email)
    return activate_license(data)


@app.get("/admin/activations")
def admin_list_activations(api_key: str, license_key: Optional[str] = None, device_id: Optional[str] = None, limit: int = 200, offset: int = 0):
    if api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Não autorizado")

    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))

    where = []
    params: List[Any] = []

    if license_key:
        where.append("l.license_key = %s")
        params.append(license_key.strip())

    if device_id:
        where.append("a.device_id = %s")
        params.append(device_id.strip())

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  l.license_key,
                  l.license_type,
                  l.status,
                  a.device_id,
                  a.buyer_email,
                  a.license_type as activation_type,
                  a.activated_at,
                  a.expires_at
                FROM public.license_activations a
                JOIN public.licenses l ON l.id = a.license_id
                {where_sql}
                ORDER BY a.activated_at DESC
                LIMIT %s OFFSET %s;
                """,
                (*params, limit, offset),
            )
            items = fetchall_dict(cur)

    return {"ok": True, "limit": limit, "offset": offset, "items": items}
