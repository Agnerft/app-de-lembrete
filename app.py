from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urlparse
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pywebpush import WebPushException, webpush
from fastapi import Request
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

API_KEY = os.getenv("PAINEL_BEST_API_KEY", "")
API_BASE_URL = os.getenv("PAINEL_BEST_BASE_URL", "https://api.painel.best/lines/")
PAGAMENTO_BUSCAR_URL = os.getenv("PAGAMENTO_BUSCAR_URL", "http://191.252.182.241:8080/buscar")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").replace("\\n", "\n")
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "https://acesso.megaapp.tech").rstrip("/")
VAPID_CLAIM_EMAIL = os.getenv("VAPID_CLAIM_EMAIL", "admin@acesso.megaapp.tech")
DATA_DIR = Path(os.getenv("APP_DATA_DIR", str(BASE_DIR / "data")))
DB_FILE = DATA_DIR / "mega_app.sqlite3"
SUBSCRIPTIONS_FILE = DATA_DIR / "push_subscriptions.json"
CLIENTS_FILE = DATA_DIR / "notification_clients.json"
SENT_REMINDERS_FILE = DATA_DIR / "sent_reminders.json"
SUPPORT_CONTACTS_FILE = DATA_DIR / "support_contacts.json"
RESELLER_LOGINS_FILE = Path(
    os.getenv("RESELLER_LOGINS_FILE", "/opt/revendas/repo_revendas/revendas_logins.json")
)
REMINDER_DAYS = {3, 2, 1, 0}
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "20"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "600"))
PHONE_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("PHONE_RATE_LIMIT_MAX_REQUESTS", "6"))
PHONE_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("PHONE_RATE_LIMIT_WINDOW_SECONDS", str(RATE_LIMIT_WINDOW_SECONDS)))
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "1800"))
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET", "") or API_KEY or VAPID_PRIVATE_KEY
REMINDER_ADMIN_TOKEN = os.getenv("REMINDER_ADMIN_TOKEN", "")
TRUSTED_PROXY_IPS = {
    item.strip() for item in os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",") if item.strip()
}
ALLOWED_HOSTS = [
    item.strip()
    for item in os.getenv("ALLOWED_HOSTS", "acesso.megaapp.tech,localhost,127.0.0.1,testserver").split(",")
    if item.strip()
]
DB_LOCK = threading.Lock()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("mega_app")

app = FastAPI(title="Lembrete de Vencimento")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
        "object-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; manifest-src 'self'; worker-src 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    if request.url.scheme == "https" or forwarded_proto == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.on_event("startup")
def start_reminder_worker() -> None:
    ensure_data_dir()
    init_database()
    thread = threading.Thread(target=reminder_loop, daemon=True)
    thread.start()


class PhoneRequest(BaseModel):
    telefone: str | None = None
    termo: str | None = None
    login: str | None = None
    access_token: str | None = None


class NotificationSubscriptionRequest(BaseModel):
    telefone: str
    cliente: dict[str, Any]
    subscription: dict[str, Any]
    access_token: str | None = None
    reminder_days: list[int] | None = None


class PixRequest(BaseModel):
    link: str


class AppPreferenceRequest(BaseModel):
    telefone: str | None = None
    login: str | None = None
    app_usado: str
    observacao: str | None = None
    access_token: str | None = None
    slot: int = 1


class AdminAuditToggleRequest(BaseModel):
    enabled: bool


class AdminSupportContactRequest(BaseModel):
    whatsapp: str = ""


class CommunityRequest(BaseModel):
    device_id: str
    liked: bool | None = None


def only_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def encode_token_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_token_part(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_access_token(telefone: Any, login: Any) -> str:
    if not ACCESS_TOKEN_SECRET:
        raise HTTPException(status_code=503, detail="Proteção de acesso ainda não configurada no servidor.")
    payload = {
        "keys": phone_lookup_keys(telefone, login),
        "login": clean_text(login),
        "exp": int(time.time()) + ACCESS_TOKEN_TTL_SECONDS,
    }
    encoded = encode_token_part(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = hmac.new(ACCESS_TOKEN_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{encode_token_part(signature)}"


def require_access_token(token: str | None, telefone: Any = None, login: Any = None) -> None:
    if not token or not ACCESS_TOKEN_SECRET:
        raise HTTPException(status_code=401, detail="Autorizacao da consulta ausente.")
    try:
        encoded, provided_signature = token.split(".", 1)
        expected_signature = hmac.new(
            ACCESS_TOKEN_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(decode_token_part(provided_signature), expected_signature):
            raise ValueError("invalid signature")
        payload = json.loads(decode_token_part(encoded))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Autorização da consulta inválida.") from None

    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=401, detail="A consulta expirou. Pesquise o telefone novamente.")

    requested_keys = set(phone_lookup_keys(telefone, login))
    token_keys = set(payload.get("keys") or [])
    login_matches = bool(clean_text(login) and clean_text(login) == clean_text(payload.get("login")))
    if not requested_keys.intersection(token_keys) and not login_matches:
        raise HTTPException(status_code=403, detail="Esta autorização não pertence ao cliente informado.")


def require_admin_token(authorization: str | None) -> None:
    if not REMINDER_ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Token administrativo não configurado.")
    scheme, _, token = clean_text(authorization).partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, REMINDER_ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Autorização administrativa inválida.")


def client_ip(request: Request) -> str:
    peer_ip = request.client.host if request.client else "unknown"
    if peer_ip not in TRUSTED_PROXY_IPS:
        return peer_ip

    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip

    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return peer_ip


def record_rate_limit_hit(
    key: str,
    max_requests: int,
    window_seconds: int,
    detail: str,
) -> None:
    now = time.time()
    window_start = now - window_seconds

    with DB_LOCK, db_connect() as conn:
        conn.execute("DELETE FROM rate_limit_hits WHERE hit_at < ?", (window_start,))
        row = conn.execute(
            "SELECT COUNT(*) AS total, MIN(hit_at) AS oldest FROM rate_limit_hits WHERE bucket_key = ? AND hit_at >= ?",
            (key, window_start),
        ).fetchone()
        if row["total"] >= max_requests:
            retry_after = max(1, int(window_seconds - (now - row["oldest"])))
            raise HTTPException(
                status_code=429,
                detail=detail,
                headers={"Retry-After": str(retry_after)},
            )
        conn.execute("INSERT INTO rate_limit_hits (bucket_key, hit_at) VALUES (?, ?)", (key, now))


def enforce_rate_limit(request: Request, phone: str) -> None:
    normalized_phone = only_digits(phone)

    record_rate_limit_hit(
        f"ip:{client_ip(request)}",
        RATE_LIMIT_MAX_REQUESTS,
        RATE_LIMIT_WINDOW_SECONDS,
        "Muitas consultas em pouco tempo. Aguarde alguns minutos e tente novamente.",
    )

    if normalized_phone:
        record_rate_limit_hit(
            f"phone:{normalized_phone}",
            PHONE_RATE_LIMIT_MAX_REQUESTS,
            PHONE_RATE_LIMIT_WINDOW_SECONDS,
            "Este telefone foi consultado muitas vezes. Aguarde alguns minutos e tente novamente.",
        )


def has_phone_area_code(phone: str) -> bool:
    digits = only_digits(phone)
    if digits.startswith("55") and len(digits) > 11:
        digits = digits[2:]
    return len(digits) >= 10


def phone_search_variants(value: Any) -> list[str]:
    digits = only_digits(value)
    if not digits:
        return []

    variants = []

    def add_variant(candidate: str) -> None:
        if candidate and candidate not in variants:
            variants.append(candidate)

    add_variant(digits)
    country = ""
    number = digits
    if digits.startswith("55") and len(digits) > 11:
        country = "55"
        number = digits[2:]
        add_variant(number)

    if len(number) in (10, 11):
        ddd = number[:2]
        subscriber = number[2:]

        if len(subscriber) == 9 and subscriber.startswith("9"):
            without_ninth = ddd + subscriber[1:]
            add_variant(without_ninth)
            add_variant(country + without_ninth)

        if len(subscriber) == 8:
            with_ninth = ddd + "9" + subscriber
            add_variant(with_ninth)
            add_variant(country + with_ninth)

    if not country and len(digits) in (10, 11):
        add_variant("55" + digits)

    return variants


def phone_lookup_keys(*values: Any) -> list[str]:
    keys = []

    def add_key(candidate: str) -> None:
        if candidate and candidate not in keys:
            keys.append(candidate)

    for value in values:
        digits = only_digits(value)
        add_key(digits)
        for variant in phone_search_variants(digits):
            add_key(variant)

    return keys


def build_clouddy_access(*id_candidates: Any) -> dict[str, str] | None:
    for candidate in id_candidates:
        cliente_id = only_digits(candidate)
        if cliente_id:
            return {"email": f"{cliente_id}@zt.rpa", "senha": cliente_id}
    return None


def format_timestamp(value: Any) -> str | None:
    if value in (None, "", "N/A", "nao_encontrado"):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%d/%m/%Y")
    except (TypeError, ValueError, OSError):
        return None


def format_expiration(value: Any) -> str | None:
    text = clean_text(value)
    if not text or text in {"N/A", "nao_encontrado"}:
        return None
    if text.isdigit():
        return format_timestamp(text)
    return text


def parse_br_date(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text or text in {"N/A", "nao_encontrado"}:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def due_status(vencimento: str | None) -> dict[str, str]:
    due_date = parse_br_date(vencimento)
    if not due_date:
        return {"label": "Sem data", "kind": "neutral"}

    diff = (due_date.date() - datetime.now().date()).days
    if diff < 0:
        return {"label": "Vencido", "kind": "danger"}
    if diff <= 5:
        return {"label": "A vencer", "kind": "warning"}
    return {"label": "Em dia", "kind": "success"}


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def db_connect():
    ensure_data_dir()
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database() -> None:
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_app_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_key TEXT NOT NULL,
                login TEXT NOT NULL DEFAULT '',
                telefone TEXT NOT NULL DEFAULT '',
                app_usado TEXT NOT NULL,
                observacao TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(phone_key, login)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_client_app_preferences_phone ON client_app_preferences(phone_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_client_app_preferences_login ON client_app_preferences(login)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_app_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_key TEXT NOT NULL,
                login TEXT NOT NULL DEFAULT '',
                telefone TEXT NOT NULL DEFAULT '',
                slot INTEGER NOT NULL,
                app_usado TEXT NOT NULL,
                observacao TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(phone_key, login, slot)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_client_app_slots_phone ON client_app_slots(phone_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_client_app_slots_login ON client_app_slots(login)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        migrated = conn.execute(
            "SELECT value FROM app_schema_meta WHERE key = 'app_slots_v1'"
        ).fetchone()
        if not migrated:
            conn.execute(
                """
                INSERT OR IGNORE INTO client_app_slots
                    (phone_key, login, telefone, slot, app_usado, observacao, created_at, updated_at)
                SELECT phone_key, login, telefone, 1, app_usado, observacao, created_at, updated_at
                FROM client_app_preferences
                WHERE app_usado != ''
                """
            )
            conn.execute(
                "INSERT INTO app_schema_meta (key, value) VALUES ('app_slots_v1', 'done')"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resellers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_user_id TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                support_whatsapp TEXT NOT NULL DEFAULT '',
                line_count INTEGER NOT NULL DEFAULT 0,
                active_line_count INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_resellers_username ON resellers(username)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_resellers_source_user_id ON resellers(source_user_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket_key TEXT NOT NULL,
                hit_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_limit_hits_bucket_time ON rate_limit_hits(bucket_key, hit_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled_until REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO admin_audit_state (id, enabled_until) VALUES (1, 0)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL,
                reseller TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_events_id ON admin_audit_events(id DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS community_visitors (
                device_id TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS community_likes (
                device_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                FOREIGN KEY(device_id) REFERENCES community_visitors(device_id) ON DELETE CASCADE
            )
            """
        )


def normalize_device_id(value: Any) -> str:
    device_id = clean_text(value).lower()
    if not re.fullmatch(r"[a-z0-9-]{16,64}", device_id):
        raise HTTPException(status_code=400, detail="Identificador do dispositivo inválido.")
    return device_id


def community_stats(device_id: Any) -> dict[str, Any]:
    normalized_id = normalize_device_id(device_id)
    with DB_LOCK, db_connect() as conn:
        users = conn.execute("SELECT COUNT(*) FROM community_visitors").fetchone()[0]
        likes = conn.execute("SELECT COUNT(*) FROM community_likes").fetchone()[0]
        liked = conn.execute(
            "SELECT 1 FROM community_likes WHERE device_id = ?", (normalized_id,)
        ).fetchone() is not None
    return {"users": users, "likes": likes, "liked": liked}


def record_community_visit(device_id: Any) -> dict[str, Any]:
    normalized_id = normalize_device_id(device_id)
    now = datetime.now(timezone.utc).isoformat()
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO community_visitors (device_id, first_seen_at, last_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (normalized_id, now, now),
        )
    return community_stats(normalized_id)


def set_community_like(device_id: Any, liked: bool) -> dict[str, Any]:
    normalized_id = normalize_device_id(device_id)
    record_community_visit(normalized_id)
    with DB_LOCK, db_connect() as conn:
        if liked:
            conn.execute(
                "INSERT OR IGNORE INTO community_likes (device_id, created_at) VALUES (?, ?)",
                (normalized_id, datetime.now(timezone.utc).isoformat()),
            )
        else:
            conn.execute("DELETE FROM community_likes WHERE device_id = ?", (normalized_id,))
    return community_stats(normalized_id)


def admin_audit_status() -> dict[str, Any]:
    with DB_LOCK, db_connect() as conn:
        row = conn.execute("SELECT enabled_until FROM admin_audit_state WHERE id = 1").fetchone()
    enabled_until = float(row["enabled_until"]) if row else 0
    return {"enabled": enabled_until > time.time(), "enabled_until": enabled_until}


def set_admin_audit(enabled: bool) -> dict[str, Any]:
    enabled_until = time.time() + 1800 if enabled else 0
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO admin_audit_state (id, enabled_until) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET enabled_until = excluded.enabled_until",
            (enabled_until,),
        )
    return {"enabled": enabled, "enabled_until": enabled_until}


def record_admin_audit_event(login: Any, reseller: Any, source: str = "consulta") -> bool:
    state = admin_audit_status()
    if not state["enabled"]:
        return False

    normalized_login = clean_text(login) or "N/A"
    normalized_reseller = clean_text(reseller) or "Não identificado"
    created_at = datetime.now(timezone.utc).isoformat()
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO admin_audit_events (login, reseller, source, created_at) VALUES (?, ?, ?, ?)",
            (normalized_login, normalized_reseller, clean_text(source), created_at),
        )
        conn.execute(
            "DELETE FROM admin_audit_events WHERE id NOT IN "
            "(SELECT id FROM admin_audit_events ORDER BY id DESC LIMIT 100)"
        )
    LOGGER.info(
        "wwp_audit login=%s reseller=%s source=%s",
        normalized_login,
        normalized_reseller,
        clean_text(source),
    )
    return True


def list_admin_audit_events(limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 100))
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            "SELECT id, login, reseller, source, created_at FROM admin_audit_events ORDER BY id DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def clear_admin_audit_events() -> None:
    with DB_LOCK, db_connect() as conn:
        conn.execute("DELETE FROM admin_audit_events")


def rows_to_app_preference(rows: list[sqlite3.Row]) -> dict[str, Any] | None:
    if not rows:
        return None
    apps = [
        {
            "slot": row["slot"],
            "app_usado": row["app_usado"],
            "observacao": row["observacao"],
            "updated_at": row["updated_at"],
        }
        for row in sorted(rows, key=lambda item: item["slot"])
    ]
    return {
        "apps": apps,
        "app_usado": apps[0]["app_usado"],
        "telefone": rows[0]["telefone"],
        "login": rows[0]["login"],
        "updated_at": max(row["updated_at"] for row in rows),
    }


def get_app_preference(telefone: Any = None, login: Any = None) -> dict[str, Any] | None:
    keys = phone_lookup_keys(telefone, login)
    login_clean = clean_text(login)
    with DB_LOCK, db_connect() as conn:
        for key in keys:
            if login_clean:
                rows = conn.execute(
                    """
                    SELECT * FROM client_app_slots
                    WHERE phone_key = ? AND login = ?
                    ORDER BY slot
                    """,
                    (key, login_clean),
                ).fetchall()
                if rows:
                    return rows_to_app_preference(rows)

            rows = conn.execute(
                """
                SELECT * FROM client_app_slots
                WHERE phone_key = ?
                ORDER BY slot
                """,
                (key,),
            ).fetchall()
            if rows:
                return rows_to_app_preference(rows)

        if login_clean:
            rows = conn.execute(
                """
                SELECT * FROM client_app_slots
                WHERE login = ?
                ORDER BY slot
                """,
                (login_clean,),
            ).fetchall()
            return rows_to_app_preference(rows)
    return None


def save_app_preference_record(request: AppPreferenceRequest) -> dict[str, Any]:
    app_usado = clean_text(request.app_usado)
    if request.slot not in {1, 2, 3}:
        raise HTTPException(status_code=400, detail="A tela deve estar entre 1 e 3.")

    telefone = clean_text(request.telefone)
    login = clean_text(request.login)
    phone_keys = phone_lookup_keys(telefone, login)
    phone_key = phone_keys[0] if phone_keys else only_digits(telefone or login)
    if not phone_key and not login:
        raise HTTPException(status_code=400, detail="Informe telefone ou login para salvar o app.")

    now = datetime.now(timezone.utc).isoformat()
    observacao = clean_text(request.observacao)
    with DB_LOCK, db_connect() as conn:
        if app_usado:
            conn.execute(
                """
                INSERT INTO client_app_slots
                    (phone_key, login, telefone, slot, app_usado, observacao, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone_key, login, slot) DO UPDATE SET
                    telefone = excluded.telefone,
                    app_usado = excluded.app_usado,
                    observacao = excluded.observacao,
                    updated_at = excluded.updated_at
                """,
                (phone_key, login, telefone, request.slot, app_usado, observacao, now, now),
            )
        else:
            conn.execute(
                "DELETE FROM client_app_slots WHERE phone_key = ? AND login = ? AND slot = ?",
                (phone_key, login, request.slot),
            )

    return {
        "app_usado": app_usado,
        "observacao": observacao,
        "telefone": telefone,
        "login": login,
        "slot": request.slot,
        "updated_at": now,
    }


def read_json_file(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json_file(path: Path, value: Any) -> None:
    ensure_data_dir()
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_whatsapp_number(value: Any) -> str:
    digits = only_digits(value)
    if len(digits) in (10, 11):
        return "55" + digits
    return digits


def whatsapp_support_url(number: str, usuario: str = "", vencimento: str = "") -> str:
    details = []
    if usuario and usuario != "N/A":
        details.append(f"Usuário: {usuario}")
    if vencimento and vencimento != "N/A":
        details.append(f"Vencimento: {vencimento}")
    details.append("Mensagem enviada pelo Mega App")

    message = "Olá, preciso de suporte com meu acesso."
    if details:
        message = f"{message} {'; '.join(details)}."
    from urllib.parse import quote

    return f"https://wa.me/{number}?text={quote(message)}"


def get_reseller_support_whatsapp(revenda: Any) -> str:
    revenda_key = normalize_key(revenda)
    if not revenda_key:
        return ""

    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            """
            SELECT username, display_name, support_whatsapp
            FROM resellers
            WHERE support_whatsapp != ''
            """
        ).fetchall()

    for row in rows:
        if normalize_key(row["username"]) == revenda_key or normalize_key(row["display_name"]) == revenda_key:
            return clean_text(row["support_whatsapp"])
    return ""


def support_contact_for_reseller(
    revenda: Any,
    usuario: Any = "",
    vencimento: Any = "",
) -> dict[str, str] | None:
    config = read_json_file(SUPPORT_CONTACTS_FILE, {})
    if not isinstance(config, dict):
        config = {}

    revenda_text = clean_text(revenda)
    revenda_key = normalize_key(revenda_text)
    contacts = config.get("revendas") if isinstance(config.get("revendas"), dict) else config
    raw_number = get_reseller_support_whatsapp(revenda_text)

    if not raw_number and revenda_key:
        for key, value in contacts.items():
            if normalize_key(key) == revenda_key:
                raw_number = value
                break

    if not raw_number:
        raw_number = config.get("default") or os.getenv("SUPPORT_WHATSAPP_DEFAULT", "")

    number = normalize_whatsapp_number(raw_number)
    if len(number) < 12:
        return None

    return {
        "revenda": revenda_text,
        "whatsapp": number,
        "url": whatsapp_support_url(
            number,
            clean_text(usuario),
            clean_text(vencimento),
        ),
    }


def validate_support_whatsapp(value: Any) -> str:
    number = normalize_whatsapp_number(value)
    if number and not 12 <= len(number) <= 15:
        raise HTTPException(status_code=400, detail="Informe um WhatsApp valido, com DDD.")
    return number


def allowed_reseller_names() -> list[str]:
    rows = read_json_file(RESELLER_LOGINS_FILE, [])
    if not isinstance(rows, list):
        return []

    names = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = clean_text(row.get("nome"))
        key = normalize_key(name)
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def sync_allowed_resellers() -> list[str]:
    names = allowed_reseller_names()
    if not names:
        return []

    now = datetime.now(timezone.utc).isoformat()
    with DB_LOCK, db_connect() as conn:
        for name in names:
            conn.execute(
                """
                INSERT INTO resellers
                    (username, display_name, first_seen_at, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    display_name = excluded.display_name,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (name, name, now, now, now),
            )
    return names


def list_admin_support_contacts() -> dict[str, Any]:
    config = read_json_file(SUPPORT_CONTACTS_FILE, {})
    if not isinstance(config, dict):
        config = {}
    official = normalize_whatsapp_number(config.get("default") or os.getenv("SUPPORT_WHATSAPP_DEFAULT", ""))

    allowed_names = sync_allowed_resellers()
    with DB_LOCK, db_connect() as conn:
        if allowed_names:
            placeholders = ",".join("?" for _ in allowed_names)
            rows = conn.execute(
                f"""
                SELECT username, display_name, support_whatsapp, line_count, active_line_count
                FROM resellers
                WHERE username IN ({placeholders})
                ORDER BY display_name COLLATE NOCASE, username COLLATE NOCASE
                """,
                allowed_names,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT username, display_name, support_whatsapp, line_count, active_line_count
                FROM resellers
                ORDER BY display_name COLLATE NOCASE, username COLLATE NOCASE
                """
            ).fetchall()

    return {
        "oficial": official,
        "revendas": [
            {
                "username": clean_text(row["username"]),
                "nome": clean_text(row["display_name"]) or clean_text(row["username"]),
                "whatsapp": normalize_whatsapp_number(row["support_whatsapp"]),
                "linhas": int(row["line_count"] or 0),
                "linhas_ativas": int(row["active_line_count"] or 0),
            }
            for row in rows
        ],
    }


def save_official_support_whatsapp(value: Any) -> str:
    number = validate_support_whatsapp(value)
    with DB_LOCK:
        config = read_json_file(SUPPORT_CONTACTS_FILE, {})
        if not isinstance(config, dict):
            config = {}
        config["default"] = number
        write_json_file(SUPPORT_CONTACTS_FILE, config)
    return number


def save_reseller_support_whatsapp(username: Any, value: Any) -> str:
    reseller = clean_text(username)
    number = validate_support_whatsapp(value)
    with DB_LOCK, db_connect() as conn:
        result = conn.execute(
            "UPDATE resellers SET support_whatsapp = ?, updated_at = ? WHERE username = ?",
            (number, datetime.now(timezone.utc).isoformat(), reseller),
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=404, detail="Revenda não encontrada.")
    return number


def reminder_key(phone: str, due_date: datetime, days_left: int) -> str:
    return f"{only_digits(phone)}:{due_date.strftime('%Y-%m-%d')}:{days_left}"


def normalize_reminder_days(value: Any = None) -> list[int]:
    if value is None:
        return sorted(REMINDER_DAYS, reverse=True)
    normalized = set()
    for item in value if isinstance(value, (list, tuple, set)) else []:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if day in REMINDER_DAYS:
            normalized.add(day)
    return sorted(normalized, reverse=True)


def get_reminder_days_for_client(*values: Any) -> list[int]:
    subscriptions = read_json_file(SUBSCRIPTIONS_FILE, {})
    for key_candidate in phone_lookup_keys(*values):
        record = subscriptions.get(key_candidate)
        if isinstance(record, dict) and record.get("subscription"):
            return normalize_reminder_days(record.get("reminder_days"))
    return sorted(REMINDER_DAYS, reverse=True)


def save_notification_client(cliente: dict[str, Any]) -> None:
    phone = only_digits(cliente.get("telefone") or cliente.get("login"))
    vencimento = clean_text(cliente.get("vencimento"))
    if not phone or not parse_br_date(vencimento):
        return

    clients = read_json_file(CLIENTS_FILE, {})
    clients[phone] = {
        "telefone": clean_text(cliente.get("telefone")),
        "login": clean_text(cliente.get("login")),
        "vencimento": vencimento,
        "plano": clean_text(cliente.get("plano")),
        "link_pagamento": clean_text(cliente.get("link_pagamento")),
        "revenda": clean_text(cliente.get("revenda")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json_file(CLIENTS_FILE, clients)


def send_push(subscription: dict[str, Any], title: str, body: str, url: str = "/") -> bool:
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return False

    target_url = clean_text(url) or "/"
    if target_url.startswith("/"):
        target_url = f"{APP_PUBLIC_URL}{target_url}"

    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "url": target_url,
            "icon": f"{APP_PUBLIC_URL}/static/assets/site-icon-192.png",
            "badge": f"{APP_PUBLIC_URL}/static/assets/site-icon-192.png",
        },
        ensure_ascii=False,
    )
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{VAPID_CLAIM_EMAIL}"},
            ttl=60 * 60 * 24,
        )
        return True
    except WebPushException:
        return False


def check_and_send_reminders() -> dict[str, int]:
    clients = read_json_file(CLIENTS_FILE, {})
    subscriptions = read_json_file(SUBSCRIPTIONS_FILE, {})
    sent = read_json_file(SENT_REMINDERS_FILE, {})
    today = datetime.now().date()
    stats = {"checked": 0, "sent": 0, "skipped": 0}

    for phone, client in clients.items():
        stats["checked"] += 1
        due_date = parse_br_date(client.get("vencimento"))
        if not due_date:
            stats["skipped"] += 1
            continue

        days_left = (due_date.date() - today).days
        if days_left not in REMINDER_DAYS:
            stats["skipped"] += 1
            continue

        key = reminder_key(phone, due_date, days_left)
        if sent.get(key):
            stats["skipped"] += 1
            continue

        subscription = None
        reminder_days = sorted(REMINDER_DAYS, reverse=True)
        for key_candidate in phone_lookup_keys(phone, client.get("telefone"), client.get("login")):
            subscription_record = subscriptions.get(key_candidate, {})
            subscription = subscription_record.get("subscription")
            if subscription:
                reminder_days = normalize_reminder_days(subscription_record.get("reminder_days"))
                break
        if not subscription:
            stats["skipped"] += 1
            continue
        if days_left not in reminder_days:
            stats["skipped"] += 1
            continue

        if days_left == 0:
            when = "vence hoje"
        elif days_left == 1:
            when = "vence em 1 dia"
        else:
            when = f"vence em {days_left} dias"
        ok = send_push(
            subscription,
            "Mega App: lembrete de vencimento",
            f"Seu acesso {when}. Toque para abrir o pagamento.",
            client.get("link_pagamento") or "/",
        )
        if ok:
            sent[key] = datetime.now(timezone.utc).isoformat()
            stats["sent"] += 1
        else:
            stats["skipped"] += 1

    write_json_file(SENT_REMINDERS_FILE, sent)
    return stats


def reminder_loop() -> None:
    while True:
        try:
            check_and_send_reminders()
        except Exception:
            pass
        time.sleep(60 * 60)


def has_payment_result(payload: dict[str, Any]) -> bool:
    not_found = {"", "N/A", "nao_encontrado", "não encontrado", "None"}
    return clean_text(payload.get("DT_RowId")) not in not_found or clean_text(payload.get("Link")) not in not_found


def payment_match_score(payload: dict[str, Any], phone: str) -> tuple[int, int]:
    searched = set(phone_search_variants(phone) or [only_digits(phone)])
    searched_suffixes = {item[-8:] for item in searched if len(item) >= 8}
    fields = [
        only_digits(payload.get("telefone")),
        only_digits(payload.get("nome")),
        only_digits(payload.get("login")),
        only_digits(payload.get("username")),
    ]

    score = 0
    for field in fields:
        if not field:
            continue
        if field in searched:
            score = max(score, 100)
        if any(field.endswith(suffix) for suffix in searched_suffixes):
            score = max(score, 80)
        if any(candidate.endswith(field[-8:]) for candidate in searched if len(field) >= 8):
            score = max(score, 70)

    try:
        expiration = int(clean_text(payload.get("data_expiracao")))
    except ValueError:
        expiration = 0

    if clean_text(payload.get("Link")) not in {"", "N/A", "nao_encontrado"}:
        score += 10

    return score, expiration


def search_payment_data(phone: str) -> dict[str, Any] | None:
    searches = phone_search_variants(phone) or [phone.strip()]
    if phone.strip() not in searches:
        searches.append(phone.strip())

    candidates = []
    for term in searches:
        try:
            response = requests.post(
                PAGAMENTO_BUSCAR_URL,
                json={"termo": term},
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise HTTPException(status_code=503, detail=f"Erro ao consultar link de pagamento: {exc}") from exc

        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail="A API de pagamento recusou a consulta.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="A API de pagamento retornou resposta inválida.") from exc

        if isinstance(payload, dict):
            if has_payment_result(payload):
                score = payment_match_score(payload, phone)[0]
                if score >= 100:
                    return payload
                if score >= 70:
                    candidates.append(payload)
        elif isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                if has_payment_result(item):
                    score = payment_match_score(item, phone)[0]
                    if score >= 100:
                        return item
                    if score >= 70:
                        candidates.append(item)

    if candidates:
        return max(candidates, key=lambda item: payment_match_score(item, phone))
    return None


def search_line_data(phone: str) -> dict[str, Any] | None:
    if not API_KEY:
        return None

    variants = phone_search_variants(phone)
    searches = variants or [phone.strip()]
    if phone.strip() not in searches:
        searches.append(phone.strip())

    matching_lines: list[dict[str, Any]] = []
    seen_line_ids: set[str] = set()

    for term in searches:
        try:
            response = requests.get(
                API_BASE_URL,
                headers={"Api-Key": API_KEY},
                params={"search": term, "page": 1, "per_page": 100},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise HTTPException(status_code=503, detail=f"Erro ao consultar login e senha: {exc}") from exc

        if response.status_code >= 400:
            continue

        try:
            payload = response.json()
        except ValueError:
            continue

        results = payload.get("results") or []
        if not isinstance(results, list):
            continue

        for line in results:
            if not isinstance(line, dict):
                continue
            candidates = [only_digits(line.get("phone")), only_digits(line.get("username"))]
            if any(
                candidate and variant and (candidate.endswith(variant[-8:]) or variant.endswith(candidate[-8:]))
                for candidate in candidates
                for variant in variants
            ):
                line_id = clean_text(line.get("id") or line.get("line_id") or line.get("username"))
                if line_id not in seen_line_ids:
                    seen_line_ids.add(line_id)
                    matching_lines.append(line)

    if not matching_lines:
        return None

    return max(matching_lines, key=line_priority_score)


def line_priority_score(line: dict[str, Any]) -> tuple[int, int, int]:
    status_text = clean_text(line.get("status")).lower()
    is_enabled = line.get("is_enabled")

    active_statuses = {"active", "ativo", "enabled", "habilitado"}
    inactive_statuses = {"disabled", "desativado", "expired", "expirado", "inactive", "inativo"}

    explicit_active = is_enabled is True or status_text in active_statuses
    explicit_inactive = is_enabled is False or status_text in inactive_statuses

    try:
        expiration = int(clean_text(line.get("exp_date")))
    except ValueError:
        expiration = 0

    not_expired = expiration >= int(time.time()) if expiration else False
    active_score = 1 if explicit_active and not explicit_inactive else 0
    current_score = 1 if not_expired else 0

    return active_score, current_score, expiration


def extract_pix_code(page_html: str) -> str | None:
    patterns = [
        r'id=["\']pixCodeInput["\'][^>]*\bvalue=["\']([^"\']+)["\']',
        r'\bvalue=["\'](000201[^"\']+)["\']',
        r'(000201[0-9A-Za-z.\-/*+:\s]{80,700})',
    ]

    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        code = html.unescape(match.group(1) if match.lastindex else match.group(0))
        code = re.sub(r"\s+", "", code.strip())
        if code.startswith("000201") and len(code) >= 80:
            return code
    return None


def fetch_pix_code(payment_link: str) -> str:
    parsed = urlparse(payment_link)
    allowed_hosts = {"pagueaqui.top", "www.pagueaqui.top"}
    if parsed.scheme != "https" or parsed.netloc.lower() not in allowed_hosts:
        raise HTTPException(status_code=400, detail="Link de pagamento invalido para copiar Pix.")

    try:
        response = requests.get(
            payment_link,
            headers={"User-Agent": "MegaApp/1.0"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"Não foi possível abrir o pagamento: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="A pagina de pagamento recusou a consulta.")

    pix_code = extract_pix_code(response.text)
    if not pix_code:
        raise HTTPException(status_code=404, detail="Código Pix não encontrado no pagamento.")
    return pix_code


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/admin/diagnostico")
def admin_diagnostic() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "admin.html", headers={"Cache-Control": "no-store"})


@app.get("/admin/auditoria")
def admin_audit_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "admin.html", headers={"Cache-Control": "no-store"})


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "sw.js", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
def status() -> dict[str, Any]:
    return {
        "status": "ok",
        "api_linhas_configurada": bool(API_KEY),
        "push_configurado": bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY),
        "protecao_configurada": bool(ACCESS_TOKEN_SECRET),
    }


@app.post("/api/comunidade/visita")
def registrar_visita_comunidade(request: CommunityRequest) -> dict[str, Any]:
    return {"status": "sucesso", **record_community_visit(request.device_id)}


@app.put("/api/comunidade/curtida")
def atualizar_curtida_comunidade(request: CommunityRequest) -> dict[str, Any]:
    if request.liked is None:
        raise HTTPException(status_code=400, detail="Informe o estado da curtida.")
    return {"status": "sucesso", **set_community_like(request.device_id, request.liked)}


@app.post("/api/app-preferencia")
def salvar_app_preferencia(request: AppPreferenceRequest) -> dict[str, Any]:
    require_access_token(request.access_token, request.telefone, request.login)
    preference = save_app_preference_record(request)
    return {"status": "sucesso", "preferencia": preference}


@app.post("/api/app-preferencia/buscar")
def buscar_app_preferencia(request: PhoneRequest) -> dict[str, Any]:
    require_access_token(request.access_token, request.telefone or request.termo, request.login)
    preference = get_app_preference(request.telefone or request.termo, request.login)
    return {"status": "sucesso", "preferencia": preference}


@app.get("/api/notificacoes/config")
def notificacoes_config() -> dict[str, Any]:
    return {
        "enabled": bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY),
        "public_key": VAPID_PUBLIC_KEY,
        "dias": sorted(REMINDER_DAYS, reverse=True),
    }


@app.post("/api/notificacoes/inscrever")
def inscrever_notificacoes(request: NotificationSubscriptionRequest) -> dict[str, Any]:
    phone = only_digits(request.telefone or request.cliente.get("telefone") or request.cliente.get("login"))
    if not phone:
        raise HTTPException(status_code=400, detail="Telefone não informado para notificações.")
    if not request.subscription.get("endpoint"):
        raise HTTPException(status_code=400, detail="Inscrição de notificação inválida.")
    require_access_token(request.access_token, request.telefone or request.cliente.get("telefone"), request.cliente.get("login"))
    reminder_days = normalize_reminder_days(request.reminder_days)
    if not reminder_days:
        raise HTTPException(status_code=400, detail="Escolha pelo menos um momento para receber o lembrete.")

    save_notification_client(request.cliente)
    subscriptions = read_json_file(SUBSCRIPTIONS_FILE, {})
    subscription_record = {
        "telefone": clean_text(request.telefone),
        "subscription": request.subscription,
        "reminder_days": reminder_days,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    for key_candidate in phone_lookup_keys(phone, request.cliente.get("telefone"), request.cliente.get("login")):
        subscriptions[key_candidate] = subscription_record
    write_json_file(SUBSCRIPTIONS_FILE, subscriptions)
    test_sent = send_push(
        request.subscription,
        "Mega App: notificações ativadas",
        "Pronto. Vamos te avisar antes do vencimento.",
        "/",
    )
    return {
        "status": "sucesso",
        "message": "Lembretes ativados neste dispositivo.",
        "teste_enviado": test_sent,
        "dias": reminder_days,
    }


@app.post("/api/notificacoes/testar")
def testar_notificacao(
    request: NotificationSubscriptionRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin_token(authorization)
    phone = only_digits(request.telefone or request.cliente.get("telefone") or request.cliente.get("login"))
    if not phone:
        raise HTTPException(status_code=400, detail="Telefone não informado para notificações.")

    ok = send_push(
        request.subscription,
        "Mega App: notificações ativadas",
        "Pronto. Vamos te avisar antes do vencimento.",
        "/",
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Não foi possível enviar a notificação de teste.")
    return {"status": "sucesso"}


@app.post("/api/notificacoes/verificar")
def verificar_notificacoes(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_token(authorization)
    return {"status": "sucesso", **check_and_send_reminders()}


@app.get("/api/admin/auditoria")
def consultar_auditoria_admin(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin_token(authorization)
    return {"status": "sucesso", **admin_audit_status(), "events": list_admin_audit_events()}


@app.post("/api/admin/auditoria")
def alternar_auditoria_admin(
    request: AdminAuditToggleRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin_token(authorization)
    return {"status": "sucesso", **set_admin_audit(request.enabled)}


@app.delete("/api/admin/auditoria/eventos")
def limpar_auditoria_admin(authorization: str | None = Header(default=None)) -> dict[str, str]:
    require_admin_token(authorization)
    clear_admin_audit_events()
    return {"status": "sucesso"}


@app.get("/api/admin/contatos")
def listar_contatos_admin(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_token(authorization)
    return list_admin_support_contacts()


@app.put("/api/admin/contatos/oficial")
def salvar_contato_oficial_admin(
    request: AdminSupportContactRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    require_admin_token(authorization)
    return {"status": "sucesso", "whatsapp": save_official_support_whatsapp(request.whatsapp)}


@app.put("/api/admin/contatos/revendas/{username}")
def salvar_contato_revenda_admin(
    username: str,
    request: AdminSupportContactRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    require_admin_token(authorization)
    return {"status": "sucesso", "whatsapp": save_reseller_support_whatsapp(username, request.whatsapp)}


@app.post("/api/admin/revendas/sincronizar")
def sincronizar_revendas_admin(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_token(authorization)
    names = sync_allowed_resellers()
    if not names:
        raise HTTPException(status_code=503, detail="Arquivo de logins das revendas não encontrado.")
    return {"status": "sucesso", "total": len(names)}


@app.post("/api/pix")
def copiar_pix(request: PixRequest) -> dict[str, str]:
    return {"pix": fetch_pix_code(clean_text(request.link))}


@app.post("/api/cliente")
def consultar_cliente(request: PhoneRequest, http_request: Request) -> JSONResponse:
    telefone = clean_text(request.telefone or request.termo or request.login)
    if not telefone:
        raise HTTPException(status_code=400, detail="Informe o telefone.")
    if not has_phone_area_code(telefone):
        raise HTTPException(status_code=400, detail="Por favor, digite o telefone com o DDD.")

    enforce_rate_limit(http_request, telefone)

    with ThreadPoolExecutor(max_workers=2) as executor:
        payment_future = executor.submit(search_payment_data, telefone)
        line_future = executor.submit(search_line_data, telefone)
        payment = payment_future.result() or {}
        line = line_future.result() or {}

    if not has_payment_result(payment) and not line:
        reseller = clean_text(line.get("user_username")) if line else ""
        support = support_contact_for_reseller(
            reseller,
            clean_text(line.get("username")) or telefone,
            format_timestamp(line.get("exp_date")),
        )
        return JSONResponse(
            {
                "detail": "Não encontrei seus dados. Por favor, fale com o suporte.",
                "reason": "payment_not_found",
                "suporte": support,
            },
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    line_vencimento = format_timestamp(line.get("exp_date"))
    payment_vencimento = format_expiration(payment.get("data_expiracao"))
    vencimento = line_vencimento or payment_vencimento
    fonte_vencimento = "the_best" if line_vencimento else "pagamento"
    link = clean_text(payment.get("Link"))
    if link in {"", "N/A", "nao_encontrado"}:
        link = None
    clouddy_acesso = build_clouddy_access(
        line.get("client_id"),
        line.get("customer_id"),
        line.get("line_id"),
        line.get("id"),
        payment.get("DT_RowId"),
    )

    cliente = {
        "login": clean_text(line.get("username")) or clean_text(payment.get("nome")) or telefone,
        "senha": clean_text(line.get("password")) or "N/A",
        "telefone": clean_text(payment.get("telefone")) or clean_text(line.get("phone")) or telefone,
        "plano": clean_text(payment.get("plano")) or clean_text(line.get("plan_name")) or clean_text(line.get("type")) or "N/A",
        "telas": clean_text(line.get("max_connections")) or clean_text(line.get("connections")) or "N/A",
        "vencimento": vencimento or "N/A",
        "status_vencimento": due_status(vencimento),
        "fonte_vencimento": fonte_vencimento,
        "vencimento_pagamento": payment_vencimento or "N/A",
        "vencimento_the_best": line_vencimento or "N/A",
        "status_the_best": clean_text(line.get("status")) or "N/A",
        "conta_ativa_the_best": bool(line.get("is_enabled")) if line else None,
        "link_pagamento": link,
        "revenda": clean_text(payment.get("Revenda")) or clean_text(line.get("user_username")),
        "clouddy_acesso": clouddy_acesso,
    }
    cliente["suporte"] = support_contact_for_reseller(
        cliente.get("revenda"),
        cliente.get("login"),
        cliente.get("vencimento"),
    )
    cliente["app_preferencia"] = get_app_preference(cliente.get("telefone"), cliente.get("login"))
    cliente["lembrete_dias"] = get_reminder_days_for_client(cliente.get("telefone"), cliente.get("login"))
    save_notification_client(cliente)
    record_admin_audit_event(cliente.get("login"), cliente.get("revenda"))
    access_token = create_access_token(cliente.get("telefone"), cliente.get("login"))

    return JSONResponse(
        {
            "status": "sucesso",
            "cliente": cliente,
            "access_token": access_token,
        },
        headers={"Cache-Control": "no-store"},
    )
