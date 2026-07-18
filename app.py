from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
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
GESTOR_CLIENT_URL = os.getenv("GESTOR_CLIENT_URL", "https://app.gestorinove.com.br/api/client")
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
GESTOR_MAIN_BEARER_META_KEY = "gestor_main_bearer"
DATA_SYNC_INTERVAL_SECONDS = max(60, int(os.getenv("DATA_SYNC_INTERVAL_SECONDS", "600")))
REMINDER_CHECK_INTERVAL_SECONDS = max(60, int(os.getenv("REMINDER_CHECK_INTERVAL_SECONDS", "3600")))
DATA_SYNC_WORKERS = max(1, int(os.getenv("DATA_SYNC_WORKERS", "8")))
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

RESELLER_ALIASES = {
    "Revenda Guilherme": ["GuiMendes"],
    "Revenda Alexandre": ["Alexandre01"],
    "Revenda Bruno": ["brunosoares"],
    "Revenda David": ["david01"],
    "Revenda Luccas": ["Luccasdf"],
    "Revenda Otavio": ["joseotavio"],
    "Revenda William": ["Williamfarias"],
    "Revenda Michele": ["MicheliRibeiro"],
    "Revenda Igor": ["igor01"],
}

GESTOR_PLANS = {
    "13": {"slug": "mensal", "nome": "mensal", "valor": "R$ 29,90"},
    "14": {"slug": "bimestral", "nome": "bimestral", "valor": "R$ 49,90"},
    "15": {"slug": "trimestral", "nome": "trimestral", "valor": "R$ 74,90"},
    "16": {"slug": "semestral", "nome": "semestral", "valor": "R$ 149,90"},
}

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
def start_background_workers() -> None:
    ensure_data_dir()
    init_database()
    thread = threading.Thread(target=maintenance_loop, daemon=True)
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


class AdminGestorConfigRequest(BaseModel):
    bearer: str = ""


class PlanChangeRequest(BaseModel):
    plan_id: str
    external_id: str | None = None
    telefone: str | None = None
    login: str | None = None
    access_token: str | None = None
    current_plan_id: str | None = None


class CommunityRequest(BaseModel):
    device_id: str
    liked: bool | None = None


def only_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def base_reseller_lookup_keys(value: Any) -> set[str]:
    key = normalize_key(value)
    if not key:
        return set()
    keys = {key}
    if key.startswith("revenda") and len(key) > len("revenda"):
        keys.add(key[len("revenda"):])
    return keys


def reseller_lookup_keys(value: Any) -> set[str]:
    keys = base_reseller_lookup_keys(value)
    if not keys:
        return set()
    for canonical, aliases in RESELLER_ALIASES.items():
        alias_keys = base_reseller_lookup_keys(canonical)
        for alias in aliases:
            alias_keys.update(base_reseller_lookup_keys(alias))
        if keys.intersection(alias_keys):
            keys.update(alias_keys)
    return keys


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
                gestor_bearer TEXT NOT NULL DEFAULT '',
                line_count INTEGER NOT NULL DEFAULT 0,
                active_line_count INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        reseller_columns = {
            clean_text(row["name"])
            for row in conn.execute("PRAGMA table_info(resellers)").fetchall()
        }
        if "gestor_bearer" not in reseller_columns:
            conn.execute("ALTER TABLE resellers ADD COLUMN gestor_bearer TEXT NOT NULL DEFAULT ''")
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                lookup_key TEXT PRIMARY KEY,
                telefone TEXT NOT NULL DEFAULT '',
                subscription_json TEXT NOT NULL,
                reminder_days_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_clients (
                phone_key TEXT PRIMARY KEY,
                telefone TEXT NOT NULL DEFAULT '',
                login TEXT NOT NULL DEFAULT '',
                vencimento TEXT NOT NULL DEFAULT '',
                plano TEXT NOT NULL DEFAULT '',
                link_pagamento TEXT NOT NULL DEFAULT '',
                revenda TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_clients_due ON notification_clients(vencimento)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_reminders (
                reminder_key TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS support_contacts (
                contact_key TEXT PRIMARY KEY,
                contact_type TEXT NOT NULL,
                whatsapp TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_resellers (
                reseller_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_lines (
                source_line_id TEXT PRIMARY KEY,
                phone TEXT NOT NULL DEFAULT '',
                phone_key TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                username_key TEXT NOT NULL DEFAULT '',
                password TEXT NOT NULL DEFAULT '',
                exp_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                is_enabled INTEGER NOT NULL DEFAULT 0,
                user_id TEXT NOT NULL DEFAULT '',
                user_username TEXT NOT NULL DEFAULT '',
                plan_name TEXT NOT NULL DEFAULT '',
                max_connections TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                source_updated_at TEXT NOT NULL DEFAULT '',
                synced_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_client_lines_phone ON client_lines(phone_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_client_lines_username ON client_lines(username_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_client_lines_reseller ON client_lines(user_username)")

    import_legacy_data_once()


def get_meta_value(key: str) -> str:
    with DB_LOCK, db_connect() as conn:
        row = conn.execute("SELECT value FROM app_schema_meta WHERE key = ?", (key,)).fetchone()
    return clean_text(row["value"]) if row else ""


def set_meta_value(key: str, value: Any) -> str:
    cleaned = clean_text(value)
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_schema_meta (key, value) VALUES (?, ?)",
            (key, cleaned),
        )
    return cleaned


def normalize_bearer_token(value: Any) -> str:
    token = clean_text(value)
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()
    return token


def gestor_plan_label(plan_id: Any) -> str:
    plan = GESTOR_PLANS.get(clean_text(plan_id))
    if not plan:
        return ""
    return f"Consultoria {plan['nome']} - {plan['valor']}"


def gestor_plan_options() -> list[dict[str, str]]:
    return [
        {"plan_id": plan_id, "label": gestor_plan_label(plan_id), **plan}
        for plan_id, plan in GESTOR_PLANS.items()
    ]


def infer_gestor_plan_id(value: Any) -> str:
    text = clean_text(value).lower()
    if not text:
        return ""
    for plan_id, plan in GESTOR_PLANS.items():
        markers = (plan_id, plan["slug"], plan["nome"], plan["valor"].lower(), plan["valor"].replace("R$ ", "").lower())
        if any(marker and marker in text for marker in markers):
            return plan_id
    return ""


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


def mirror_json_dataset_to_database(dataset: str, value: Any) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with DB_LOCK, db_connect() as conn:
        if dataset == "push_subscriptions":
            conn.execute("DELETE FROM push_subscriptions")
            if isinstance(value, dict):
                for lookup_key, record in value.items():
                    if not isinstance(record, dict):
                        continue
                    conn.execute(
                        """
                        INSERT INTO push_subscriptions
                            (lookup_key, telefone, subscription_json, reminder_days_json, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            clean_text(lookup_key),
                            clean_text(record.get("telefone")),
                            json.dumps(record.get("subscription") or {}, ensure_ascii=False, separators=(",", ":")),
                            json.dumps(normalize_reminder_days(record.get("reminder_days")), separators=(",", ":")),
                            clean_text(record.get("updated_at")) or now,
                        ),
                    )
            return

        if dataset == "notification_clients":
            conn.execute("DELETE FROM notification_clients")
            if isinstance(value, dict):
                for phone_key, record in value.items():
                    if not isinstance(record, dict):
                        continue
                    conn.execute(
                        """
                        INSERT INTO notification_clients
                            (phone_key, telefone, login, vencimento, plano, link_pagamento, revenda, payload_json, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            clean_text(phone_key),
                            clean_text(record.get("telefone")),
                            clean_text(record.get("login")),
                            clean_text(record.get("vencimento")),
                            clean_text(record.get("plano")),
                            clean_text(record.get("link_pagamento")),
                            clean_text(record.get("revenda")),
                            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                            clean_text(record.get("updated_at")) or now,
                        ),
                    )
            return

        if dataset == "sent_reminders":
            conn.execute("DELETE FROM sent_reminders")
            if isinstance(value, dict):
                conn.executemany(
                    "INSERT INTO sent_reminders (reminder_key, sent_at) VALUES (?, ?)",
                    [(clean_text(key), clean_text(sent_at)) for key, sent_at in value.items() if clean_text(key)],
                )
            return

        if dataset == "support_contacts":
            conn.execute("DELETE FROM support_contacts")
            if not isinstance(value, dict):
                return
            default_number = normalize_whatsapp_number(value.get("default"))
            conn.execute(
                "INSERT INTO support_contacts (contact_key, contact_type, whatsapp, updated_at) VALUES (?, ?, ?, ?)",
                ("default", "official", default_number, now),
            )
            reseller_contacts = value.get("revendas")
            if isinstance(reseller_contacts, dict):
                for key, number in reseller_contacts.items():
                    conn.execute(
                        "INSERT INTO support_contacts (contact_key, contact_type, whatsapp, updated_at) VALUES (?, ?, ?, ?)",
                        (clean_text(key), "reseller", normalize_whatsapp_number(number), now),
                    )


def import_legacy_data_once() -> None:
    with DB_LOCK, db_connect() as conn:
        migrated = conn.execute(
            "SELECT value FROM app_schema_meta WHERE key = 'legacy_json_to_sqlite_v1'"
        ).fetchone()
    if migrated:
        return

    datasets = (
        ("push_subscriptions", SUBSCRIPTIONS_FILE),
        ("notification_clients", CLIENTS_FILE),
        ("sent_reminders", SENT_REMINDERS_FILE),
        ("support_contacts", SUPPORT_CONTACTS_FILE),
    )
    for dataset, path in datasets:
        if path.exists():
            mirror_json_dataset_to_database(dataset, read_json_file(path, {}))

    import_allowed_resellers_from_json()
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_schema_meta (key, value) VALUES ('legacy_json_to_sqlite_v1', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )


def import_allowed_resellers_from_json() -> list[str]:
    rows = read_json_file(RESELLER_LOGINS_FILE, [])
    if not isinstance(rows, list):
        return []

    names: list[str] = []
    seen: set[str] = set()
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = clean_text(row.get("nome"))
        key = normalize_key(name)
        if name and key not in seen:
            seen.add(key)
            names.append(name)

    if names:
        with DB_LOCK, db_connect() as conn:
            conn.execute("DELETE FROM allowed_resellers")
            conn.executemany(
                "INSERT INTO allowed_resellers (reseller_key, display_name, updated_at) VALUES (?, ?, ?)",
                [(normalize_key(name), name, now) for name in names],
            )
            conn.executemany(
                """
                INSERT INTO resellers
                    (username, display_name, first_seen_at, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    display_name = excluded.display_name,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                [(name, name, now, now, now) for name in names],
            )
    return names


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
    revenda_text = clean_text(revenda)
    revenda_key = normalize_key(revenda_text)
    raw_number = get_reseller_support_whatsapp(revenda_text)

    if not raw_number and revenda_key:
        with DB_LOCK, db_connect() as conn:
            row = conn.execute(
                "SELECT whatsapp FROM support_contacts WHERE contact_type = 'reseller' AND contact_key = ?",
                (revenda_text,),
            ).fetchone()
            if not row:
                rows = conn.execute(
                    "SELECT contact_key, whatsapp FROM support_contacts WHERE contact_type = 'reseller'"
                ).fetchall()
                row = next((item for item in rows if normalize_key(item["contact_key"]) == revenda_key), None)
        raw_number = clean_text(row["whatsapp"]) if row else ""

    if not raw_number:
        with DB_LOCK, db_connect() as conn:
            row = conn.execute(
                "SELECT whatsapp FROM support_contacts WHERE contact_key = 'default'"
            ).fetchone()
        raw_number = clean_text(row["whatsapp"]) if row else os.getenv("SUPPORT_WHATSAPP_DEFAULT", "")

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
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            "SELECT display_name FROM allowed_resellers ORDER BY display_name COLLATE NOCASE"
        ).fetchall()
    return [clean_text(row["display_name"]) for row in rows]


def sync_allowed_resellers() -> list[str]:
    names = import_allowed_resellers_from_json() or allowed_reseller_names()
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
    with DB_LOCK, db_connect() as conn:
        official_row = conn.execute(
            "SELECT whatsapp FROM support_contacts WHERE contact_key = 'default'"
        ).fetchone()
    official = normalize_whatsapp_number(
        clean_text(official_row["whatsapp"]) if official_row else os.getenv("SUPPORT_WHATSAPP_DEFAULT", "")
    )

    allowed_names = allowed_reseller_names()
    if not allowed_names and RESELLER_LOGINS_FILE.exists():
        allowed_names = import_allowed_resellers_from_json()
    with DB_LOCK, db_connect() as conn:
        if allowed_names:
            placeholders = ",".join("?" for _ in allowed_names)
            rows = conn.execute(
                f"""
                SELECT username, display_name, support_whatsapp, gestor_bearer, line_count, active_line_count
                FROM resellers
                WHERE username IN ({placeholders}) OR line_count > 0
                ORDER BY display_name COLLATE NOCASE, username COLLATE NOCASE
                """,
                allowed_names,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT username, display_name, support_whatsapp, gestor_bearer, line_count, active_line_count
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
                "gestor_configurado": bool(clean_text(row["gestor_bearer"])),
                "linhas": int(row["line_count"] or 0),
                "linhas_ativas": int(row["active_line_count"] or 0),
            }
            for row in rows
        ],
    }


def save_official_support_whatsapp(value: Any) -> str:
    number = validate_support_whatsapp(value)
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO support_contacts (contact_key, contact_type, whatsapp, updated_at)
            VALUES ('default', 'official', ?, ?)
            ON CONFLICT(contact_key) DO UPDATE SET
                contact_type = excluded.contact_type,
                whatsapp = excluded.whatsapp,
                updated_at = excluded.updated_at
            """,
            (number, datetime.now(timezone.utc).isoformat()),
        )
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


def get_reseller_gestor_bearer(revenda: Any) -> str:
    revenda_keys = reseller_lookup_keys(revenda)
    if revenda_keys:
        with DB_LOCK, db_connect() as conn:
            try:
                rows = conn.execute("SELECT username, display_name, gestor_bearer FROM resellers").fetchall()
            except sqlite3.OperationalError:
                rows = []
        for row in rows:
            row_keys = reseller_lookup_keys(row["username"]) | reseller_lookup_keys(row["display_name"])
            if revenda_keys.intersection(row_keys):
                bearer = clean_text(row["gestor_bearer"])
                if bearer:
                    return bearer
    return get_main_gestor_bearer()


def get_main_gestor_bearer() -> str:
    return get_meta_value(GESTOR_MAIN_BEARER_META_KEY)


def save_main_gestor_bearer(value: Any) -> dict[str, Any]:
    token = normalize_bearer_token(value)
    set_meta_value(GESTOR_MAIN_BEARER_META_KEY, token)
    return {"configured": bool(token)}


def reseller_gestor_configured(revenda: Any) -> bool:
    return bool(get_reseller_gestor_bearer(revenda))


def gestor_config_status() -> dict[str, Any]:
    payload = list_admin_support_contacts()
    configured = sum(1 for reseller in payload["revendas"] if reseller.get("gestor_configurado"))
    main_configured = bool(get_main_gestor_bearer())
    return {
        "configured": configured > 0 or main_configured,
        "configured_total": configured,
        "principal_configurado": main_configured,
        "revendas": payload["revendas"],
    }


def resolve_gestor_reseller(*candidates: Any) -> str:
    cleaned = [clean_text(candidate) for candidate in candidates if clean_text(candidate)]
    for candidate in cleaned:
        if reseller_gestor_configured(candidate):
            return candidate
    return cleaned[0] if cleaned else ""


def save_gestor_bearer(username: Any, value: Any) -> dict[str, Any]:
    reseller = clean_text(username)
    token = normalize_bearer_token(value)
    with DB_LOCK, db_connect() as conn:
        result = conn.execute(
            "UPDATE resellers SET gestor_bearer = ?, updated_at = ? WHERE username = ?",
            (token, datetime.now(timezone.utc).isoformat(), reseller),
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=404, detail="Revenda nao encontrada.")
    return {"username": reseller, "configured": bool(token)}


def change_gestor_client_plan(plan_id: Any, external_id: Any, revenda: Any) -> dict[str, Any]:
    plan_id_clean = clean_text(plan_id)
    external_id_clean = clean_text(external_id)
    revenda_clean = clean_text(revenda)
    if plan_id_clean not in GESTOR_PLANS:
        raise HTTPException(status_code=400, detail="Plano informado nao e valido.")
    if not external_id_clean:
        raise HTTPException(status_code=400, detail="Cliente sem external_id da The Best.")

    bearer = get_reseller_gestor_bearer(revenda_clean)
    if not bearer:
        raise HTTPException(status_code=503, detail="Bearer do Gestor nao configurado para esta revenda nem para a API principal.")

    try:
        response = requests.patch(
            GESTOR_CLIENT_URL,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
            },
            json={"plan_id": plan_id_clean, "external_id": external_id_clean},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail="Nao foi possivel falar com o Gestor agora.") from exc

    if response.status_code >= 400:
        detail = "Gestor recusou a troca de plano."
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = clean_text(
                    payload.get("message")
                    or payload.get("detail")
                    or payload.get("error")
                    or payload.get("errors")
                    or detail
                )
            else:
                detail = clean_text(payload) or detail
        except (ValueError, AttributeError):
            detail = clean_text(response.text) or detail
        LOGGER.warning(
            "gestor_plan_change_rejected status=%s plan_id=%s external_id=%s revenda=%s detail=%s",
            response.status_code,
            plan_id_clean,
            external_id_clean,
            revenda_clean,
            detail,
        )
        raise HTTPException(status_code=502, detail=detail)

    plan_label = gestor_plan_label(plan_id_clean)
    update_cached_client_plan(external_id_clean, plan_label)

    return {
        "status": "sucesso",
        "plan_id": plan_id_clean,
        "plano": plan_label,
        "revenda": revenda_clean,
    }


def update_cached_client_plan(external_id: Any, plan_label: Any) -> None:
    external_id_clean = clean_text(external_id)
    plan_label_clean = clean_text(plan_label)
    if not external_id_clean or not plan_label_clean:
        return

    now = datetime.now(timezone.utc).isoformat()
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            """
            SELECT source_line_id, payload_json
            FROM client_lines
            WHERE source_line_id = ? OR payload_json LIKE ?
            """,
            (external_id_clean, f'%"{external_id_clean}"%'),
        ).fetchall()

        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            identifiers = {
                clean_text(payload.get("client_id")),
                clean_text(payload.get("customer_id")),
                clean_text(payload.get("line_id")),
                clean_text(payload.get("id")),
                clean_text(row["source_line_id"]),
            }
            if external_id_clean not in identifiers:
                continue

            payload["plan_name"] = plan_label_clean
            conn.execute(
                """
                UPDATE client_lines
                SET plan_name = ?, payload_json = ?, synced_at = ?
                WHERE source_line_id = ?
                """,
                (
                    plan_label_clean,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                    row["source_line_id"],
                ),
            )


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
    keys = phone_lookup_keys(*values)
    if not keys:
        return sorted(REMINDER_DAYS, reverse=True)
    placeholders = ",".join("?" for _ in keys)
    with DB_LOCK, db_connect() as conn:
        row = conn.execute(
            f"SELECT subscription_json, reminder_days_json FROM push_subscriptions WHERE lookup_key IN ({placeholders}) LIMIT 1",
            keys,
        ).fetchone()
    if row:
        try:
            subscription = json.loads(row["subscription_json"])
            days = json.loads(row["reminder_days_json"])
        except (TypeError, json.JSONDecodeError):
            subscription, days = {}, None
        if isinstance(subscription, dict) and subscription:
            return normalize_reminder_days(days)
    return sorted(REMINDER_DAYS, reverse=True)


def has_active_reminders_for_client(*values: Any) -> bool:
    keys = phone_lookup_keys(*values)
    if not keys:
        return False
    placeholders = ",".join("?" for _ in keys)
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            f"SELECT subscription_json FROM push_subscriptions WHERE lookup_key IN ({placeholders})",
            keys,
        ).fetchall()
    for row in rows:
        try:
            subscription = json.loads(row["subscription_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(subscription, dict) and subscription.get("endpoint"):
            return True
    return False


def save_notification_client(cliente: dict[str, Any]) -> None:
    phone = only_digits(cliente.get("telefone") or cliente.get("login"))
    vencimento = clean_text(cliente.get("vencimento"))
    if not phone or not parse_br_date(vencimento):
        return

    record = {
        "telefone": clean_text(cliente.get("telefone")),
        "login": clean_text(cliente.get("login")),
        "vencimento": vencimento,
        "plano": clean_text(cliente.get("plano")),
        "link_pagamento": clean_text(cliente.get("link_pagamento")),
        "revenda": clean_text(cliente.get("revenda")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO notification_clients
                (phone_key, telefone, login, vencimento, plano, link_pagamento, revenda, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone_key) DO UPDATE SET
                telefone = excluded.telefone,
                login = excluded.login,
                vencimento = excluded.vencimento,
                plano = excluded.plano,
                link_pagamento = excluded.link_pagamento,
                revenda = excluded.revenda,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                phone,
                record["telefone"],
                record["login"],
                record["vencimento"],
                record["plano"],
                record["link_pagamento"],
                record["revenda"],
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                record["updated_at"],
            ),
        )


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
    with DB_LOCK, db_connect() as conn:
        client_rows = conn.execute(
            "SELECT phone_key, telefone, login, vencimento, link_pagamento FROM notification_clients"
        ).fetchall()
    today = datetime.now().date()
    stats = {"checked": 0, "sent": 0, "skipped": 0}

    for client in client_rows:
        phone = clean_text(client["phone_key"])
        stats["checked"] += 1
        due_date = parse_br_date(client["vencimento"])
        if not due_date:
            stats["skipped"] += 1
            continue

        days_left = (due_date.date() - today).days
        if days_left not in REMINDER_DAYS:
            stats["skipped"] += 1
            continue

        key = reminder_key(phone, due_date, days_left)
        subscription = None
        reminder_days = sorted(REMINDER_DAYS, reverse=True)
        lookup_keys = phone_lookup_keys(phone, client["telefone"], client["login"])
        placeholders = ",".join("?" for _ in lookup_keys)
        with DB_LOCK, db_connect() as conn:
            subscription_rows = conn.execute(
                f"SELECT subscription_json, reminder_days_json FROM push_subscriptions WHERE lookup_key IN ({placeholders})",
                lookup_keys,
            ).fetchall()
        for row in subscription_rows:
            try:
                candidate = json.loads(row["subscription_json"])
                candidate_days = json.loads(row["reminder_days_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(candidate, dict) and candidate.get("endpoint"):
                subscription = candidate
                reminder_days = normalize_reminder_days(candidate_days)
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
        claimed_at = datetime.now(timezone.utc).isoformat()
        with DB_LOCK, db_connect() as conn:
            claimed = conn.execute(
                "INSERT OR IGNORE INTO sent_reminders (reminder_key, sent_at) VALUES (?, ?)",
                (key, claimed_at),
            ).rowcount == 1
        if not claimed:
            stats["skipped"] += 1
            continue

        ok = send_push(
            subscription,
            "Mega App: lembrete de vencimento",
            f"Seu acesso {when}. Toque para abrir o pagamento.",
            client["link_pagamento"] or "/",
        )
        if ok:
            stats["sent"] += 1
        else:
            with DB_LOCK, db_connect() as conn:
                conn.execute("DELETE FROM sent_reminders WHERE reminder_key = ? AND sent_at = ?", (key, claimed_at))
            stats["skipped"] += 1

    return stats


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


def search_payment_data_for_line(line: dict[str, Any], original_term: str) -> dict[str, Any] | None:
    tried = {only_digits(original_term)}
    for candidate in (line.get("phone"), line.get("username")):
        digits = only_digits(candidate)
        if not digits or digits in tried:
            continue
        tried.add(digits)
        payment = search_payment_data(clean_text(candidate))
        if has_payment_result(payment or {}):
            return payment
    return None


def search_line_data_remote(phone: str) -> dict[str, Any] | None:
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


def search_line_data_from_database(phone: str) -> dict[str, Any] | None:
    variants = phone_search_variants(phone)
    exact_keys = {only_digits(item) for item in variants if only_digits(item)}
    suffixes = {item[-8:] for item in exact_keys if len(item) >= 8}
    if not exact_keys and not suffixes:
        return None

    clauses: list[str] = []
    params: list[str] = []
    if exact_keys:
        placeholders = ",".join("?" for _ in exact_keys)
        clauses.extend((f"phone_key IN ({placeholders})", f"username_key IN ({placeholders})"))
        params.extend(sorted(exact_keys))
        params.extend(sorted(exact_keys))
    for suffix in sorted(suffixes):
        clauses.extend(("phone_key LIKE ?", "username_key LIKE ?"))
        params.extend((f"%{suffix}", f"%{suffix}"))

    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            f"SELECT payload_json FROM client_lines WHERE {' OR '.join(clauses)} LIMIT 100",
            params,
        ).fetchall()

    matches: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            matches.append(payload)
    return max(matches, key=line_priority_score) if matches else None


def search_line_data(phone: str) -> dict[str, Any] | None:
    local = search_line_data_from_database(phone)
    if local and line_priority_score(local)[:2] == (1, 1):
        return local
    if local:
        try:
            expiration = int(clean_text(local.get("exp_date")))
        except ValueError:
            expiration = 0
        if expiration and expiration < int(time.time()):
            remote = search_line_data_remote(phone)
            if remote:
                return max([local, remote], key=line_priority_score)
        return local
    return search_line_data_remote(phone)


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


def fetch_line_page(page: int, per_page: int = 1000) -> dict[str, Any]:
    response = requests.get(
        API_BASE_URL,
        headers={"Api-Key": API_KEY},
        params={"page": page, "per_page": per_page},
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("A API de linhas retornou um formato invalido.")
    return payload


def sync_client_lines(per_page: int = 1000, workers: int | None = None) -> dict[str, int]:
    if not API_KEY:
        raise RuntimeError("PAINEL_BEST_API_KEY nao configurada.")
    worker_count = workers or DATA_SYNC_WORKERS

    first_page = fetch_line_page(1, per_page)
    last_page = max(1, int(first_page.get("last_page") or 1))
    payloads = [first_page]
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(fetch_line_page, page, per_page) for page in range(2, last_page + 1)]
        for future in as_completed(futures):
            payloads.append(future.result())

    synced_at = datetime.now(timezone.utc).isoformat()
    lines: list[tuple[Any, ...]] = []
    reseller_counts: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        for line in payload.get("results") or []:
            if not isinstance(line, dict):
                continue
            source_line_id = clean_text(line.get("id") or line.get("line_id"))
            if not source_line_id:
                continue
            phone = clean_text(line.get("phone"))
            username = clean_text(line.get("username"))
            reseller = clean_text(line.get("user_username"))
            lines.append(
                (
                    source_line_id,
                    phone,
                    only_digits(phone),
                    username,
                    only_digits(username),
                    clean_text(line.get("password")),
                    clean_text(line.get("exp_date")),
                    clean_text(line.get("status")),
                    1 if line.get("is_enabled") is True else 0,
                    clean_text(line.get("user_id")),
                    reseller,
                    clean_text(line.get("plan_name") or line.get("type")),
                    clean_text(line.get("max_connections")),
                    json.dumps(line, ensure_ascii=False, separators=(",", ":")),
                    clean_text(line.get("updated_at")),
                    synced_at,
                )
            )
            if reseller:
                item = reseller_counts.setdefault(
                    reseller,
                    {"source_user_id": clean_text(line.get("user_id")), "total": 0, "active": 0},
                )
                item["total"] += 1
                if line.get("is_enabled") is True and clean_text(line.get("status")).lower() == "active":
                    item["active"] += 1

    with DB_LOCK, db_connect() as conn:
        conn.execute("DROP TABLE IF EXISTS client_lines_staging")
        conn.execute("CREATE TABLE client_lines_staging AS SELECT * FROM client_lines WHERE 0")
        conn.executemany(
            """
            INSERT INTO client_lines_staging
                (source_line_id, phone, phone_key, username, username_key, password, exp_date,
                 status, is_enabled, user_id, user_username, plan_name, max_connections,
                 payload_json, source_updated_at, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            lines,
        )
        conn.execute("DELETE FROM client_lines")
        conn.execute("INSERT INTO client_lines SELECT * FROM client_lines_staging")
        conn.execute("DROP TABLE client_lines_staging")

        for username, counts in reseller_counts.items():
            conn.execute(
                """
                INSERT INTO resellers
                    (source_user_id, username, display_name, line_count, active_line_count,
                     first_seen_at, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    source_user_id = excluded.source_user_id,
                    line_count = excluded.line_count,
                    active_line_count = excluded.active_line_count,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    counts["source_user_id"], username, username, counts["total"], counts["active"],
                    synced_at, synced_at, synced_at,
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO app_schema_meta (key, value) VALUES ('client_lines_last_sync', ?)",
            (synced_at,),
        )
    return {"lines": len(lines), "resellers": len(reseller_counts), "pages": last_page}


def acquire_maintenance_lease(name: str, lease_seconds: int) -> bool:
    now = time.time()
    key = f"lease:{name}"
    with DB_LOCK, db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO app_schema_meta (key, value) VALUES (?, '0')", (key,))
        result = conn.execute(
            "UPDATE app_schema_meta SET value = ? WHERE key = ? AND CAST(value AS REAL) <= ?",
            (str(now + lease_seconds), key, now),
        )
    return result.rowcount == 1


def maintenance_loop() -> None:
    next_sync = 0.0
    next_reminder_check = 0.0
    while True:
        now = time.time()
        if now >= next_sync:
            next_sync = now + DATA_SYNC_INTERVAL_SECONDS
            if acquire_maintenance_lease("client_lines_sync", DATA_SYNC_INTERVAL_SECONDS - 5):
                try:
                    stats = sync_client_lines()
                    LOGGER.info("client_lines_sync lines=%s resellers=%s pages=%s", stats["lines"], stats["resellers"], stats["pages"])
                except Exception:
                    LOGGER.exception("Falha ao sincronizar clientes no SQLite")
        if now >= next_reminder_check:
            next_reminder_check = now + REMINDER_CHECK_INTERVAL_SECONDS
            try:
                stats = check_and_send_reminders()
                LOGGER.info("reminder_check checked=%s sent=%s skipped=%s", stats["checked"], stats["sent"], stats["skipped"])
            except Exception:
                LOGGER.exception("Falha ao verificar lembretes")
        time.sleep(30)


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
    subscription_record = {
        "telefone": clean_text(request.telefone),
        "subscription": request.subscription,
        "reminder_days": reminder_days,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    lookup_keys = phone_lookup_keys(phone, request.cliente.get("telefone"), request.cliente.get("login"))
    subscription_json = json.dumps(request.subscription, ensure_ascii=False, separators=(",", ":"))
    reminder_days_json = json.dumps(reminder_days, separators=(",", ":"))
    with DB_LOCK, db_connect() as conn:
        for key_candidate in lookup_keys:
            conn.execute(
                """
                INSERT INTO push_subscriptions
                    (lookup_key, telefone, subscription_json, reminder_days_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lookup_key) DO UPDATE SET
                    telefone = excluded.telefone,
                    subscription_json = excluded.subscription_json,
                    reminder_days_json = excluded.reminder_days_json,
                    updated_at = excluded.updated_at
                """,
                (
                    key_candidate,
                    subscription_record["telefone"],
                    subscription_json,
                    reminder_days_json,
                    subscription_record["updated_at"],
                ),
            )
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


@app.get("/api/admin/gestor")
def consultar_gestor_admin(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_token(authorization)
    return {"status": "sucesso", **gestor_config_status()}


@app.put("/api/admin/gestor/principal")
def salvar_gestor_principal_admin(
    request: AdminGestorConfigRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin_token(authorization)
    return {"status": "sucesso", **save_main_gestor_bearer(request.bearer)}


@app.put("/api/admin/gestor/revendas/{username}")
def salvar_gestor_admin(
    username: str,
    request: AdminGestorConfigRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin_token(authorization)
    return {"status": "sucesso", **save_gestor_bearer(username, request.bearer)}


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


@app.post("/api/plano/trocar")
def trocar_plano_cliente(request: PlanChangeRequest) -> dict[str, Any]:
    require_access_token(request.access_token, request.telefone, request.login)
    line = search_line_data(request.telefone or request.login or "")
    try:
        payment = search_payment_data(request.telefone or request.login or "") or {}
    except HTTPException:
        payment = {}
    current_plan_id = clean_text(request.current_plan_id) or infer_gestor_plan_id(
        clean_text(line.get("plan_name")) or clean_text(line.get("type")) or clean_text(payment.get("plano"))
    )
    if current_plan_id and current_plan_id == clean_text(request.plan_id):
        raise HTTPException(status_code=400, detail="Este cliente ja esta nesse plano.")
    external_id = clean_text(
        request.external_id
        or line.get("client_id")
        or line.get("customer_id")
        or line.get("line_id")
        or line.get("id")
    )
    revenda = resolve_gestor_reseller(line.get("user_username"), payment.get("Revenda"))
    return change_gestor_client_plan(request.plan_id, external_id, revenda)


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
        payment_error: HTTPException | None = None
        line_error: HTTPException | None = None
        try:
            payment = payment_future.result() or {}
        except HTTPException as exc:
            payment_error = exc
            payment = {}
            LOGGER.warning("Payment lookup failed for phone search: %s", exc.detail)
        try:
            line = line_future.result() or {}
        except HTTPException as exc:
            line_error = exc
            line = {}
            LOGGER.warning("Line lookup failed for phone search: %s", exc.detail)

    if not has_payment_result(payment) and line:
        try:
            payment = search_payment_data_for_line(line, telefone) or payment
        except HTTPException as exc:
            LOGGER.warning("Payment lookup by line phone failed for phone search: %s", exc.detail)

    if payment_error and line_error:
        raise HTTPException(
            status_code=503,
            detail="Nao foi possivel consultar agora. Tente novamente em alguns instantes.",
        )

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
    revenda = clean_text(payment.get("Revenda")) or clean_text(line.get("user_username"))
    gestor_revenda = resolve_gestor_reseller(line.get("user_username"), revenda)

    line_plan = clean_text(line.get("plan_name")) or clean_text(line.get("type"))
    payment_plan = clean_text(payment.get("plano"))
    gestor_plan_id = infer_gestor_plan_id(line_plan or payment_plan)

    cliente = {
        "login": clean_text(line.get("username")) or clean_text(payment.get("nome")) or telefone,
        "senha": clean_text(line.get("password")) or "N/A",
        "telefone": clean_text(payment.get("telefone")) or clean_text(line.get("phone")) or telefone,
        "plano": line_plan or payment_plan or "N/A",
        "gestor_plan_id": gestor_plan_id,
        "gestor_external_id": clean_text(line.get("client_id") or line.get("customer_id") or line.get("line_id") or line.get("id")),
        "gestor_revenda": gestor_revenda,
        "gestor_planos": gestor_plan_options(),
        "gestor_configurado": reseller_gestor_configured(gestor_revenda),
        "telas": clean_text(line.get("max_connections")) or clean_text(line.get("connections")) or "N/A",
        "vencimento": vencimento or "N/A",
        "status_vencimento": due_status(vencimento),
        "fonte_vencimento": fonte_vencimento,
        "vencimento_pagamento": payment_vencimento or "N/A",
        "vencimento_the_best": line_vencimento or "N/A",
        "status_the_best": clean_text(line.get("status")) or "N/A",
        "conta_ativa_the_best": bool(line.get("is_enabled")) if line else None,
        "link_pagamento": link,
        "revenda": revenda,
        "clouddy_acesso": clouddy_acesso,
    }
    cliente["suporte"] = support_contact_for_reseller(
        cliente.get("revenda"),
        cliente.get("login"),
        cliente.get("vencimento"),
    )
    cliente["app_preferencia"] = get_app_preference(cliente.get("telefone"), cliente.get("login"))
    cliente["lembrete_dias"] = get_reminder_days_for_client(cliente.get("telefone"), cliente.get("login"))
    cliente["lembretes_ativos"] = has_active_reminders_for_client(cliente.get("telefone"), cliente.get("login"))
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
