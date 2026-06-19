from __future__ import annotations

import argparse
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import API_BASE_URL, API_KEY, DB_FILE, clean_text, init_database  # noqa: E402


def is_active_line(line: dict) -> bool:
    return line.get("is_enabled") is True and clean_text(line.get("status")).lower() == "active"


def fetch_page(page: int, per_page: int) -> dict:
    response = requests.get(
        API_BASE_URL,
        headers={"Api-Key": API_KEY},
        params={"page": page, "per_page": per_page},
        timeout=45,
    )
    response.raise_for_status()
    return response.json()


def sync_resellers(max_pages: int | None = None, per_page: int = 1000, workers: int = 8) -> list[dict]:
    if not API_KEY:
        raise RuntimeError("PAINEL_BEST_API_KEY nao configurada.")

    init_database()
    first_page = fetch_page(1, per_page)
    last_page = int(first_page.get("last_page") or 1)
    if max_pages:
        last_page = min(last_page, max_pages)

    resellers: dict[str, dict] = {}

    def collect(payload: dict) -> None:
        for line in payload.get("results") or []:
            if not isinstance(line, dict):
                continue
            username = clean_text(line.get("user_username"))
            if not username:
                continue
            item = resellers.setdefault(
                username,
                {
                    "source_user_id": clean_text(line.get("user_id")),
                    "username": username,
                    "display_name": username,
                    "line_count": 0,
                    "active_line_count": 0,
                },
            )
            item["line_count"] += 1
            if is_active_line(line):
                item["active_line_count"] += 1

    collect(first_page)
    print(f"Paginas para sincronizar: {last_page}", flush=True)

    done = 1
    pages = range(2, last_page + 1)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_page, page, per_page): page for page in pages}
        for future in as_completed(futures):
            page = futures[future]
            collect(future.result())
            done += 1
            if done == last_page or done % 10 == 0:
                print(f"Paginas processadas: {done}/{last_page}", flush=True)

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_FILE, timeout=30) as conn:
        for item in sorted(resellers.values(), key=lambda row: row["username"].lower()):
            conn.execute(
                """
                INSERT INTO resellers
                    (source_user_id, username, display_name, line_count, active_line_count, first_seen_at, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    source_user_id = excluded.source_user_id,
                    display_name = CASE
                        WHEN resellers.display_name = '' OR resellers.display_name = resellers.username
                        THEN excluded.display_name
                        ELSE resellers.display_name
                    END,
                    line_count = excluded.line_count,
                    active_line_count = excluded.active_line_count,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    item["source_user_id"],
                    item["username"],
                    item["display_name"],
                    item["line_count"],
                    item["active_line_count"],
                    now,
                    now,
                    now,
                ),
            )

    return sorted(resellers.values(), key=lambda row: row["username"].lower())


def main() -> None:
    parser = argparse.ArgumentParser(description="Sincroniza revendas da API The Best para o SQLite.")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--per-page", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    rows = sync_resellers(max_pages=args.max_pages, per_page=args.per_page, workers=args.workers)
    print(f"Revendas salvas: {len(rows)}")
    for row in rows:
        print(f"- {row['username']} ({row['line_count']} linhas, {row['active_line_count']} ativas)")


if __name__ == "__main__":
    main()
