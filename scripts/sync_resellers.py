from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import init_database, sync_client_lines  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincroniza clientes e revendas da API The Best para o SQLite."
    )
    parser.add_argument("--per-page", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    init_database()
    stats = sync_client_lines(per_page=args.per_page, workers=args.workers)
    print(f"Linhas salvas: {stats['lines']}")
    print(f"Revendas atualizadas: {stats['resellers']}")
    print(f"Paginas processadas: {stats['pages']}")


if __name__ == "__main__":
    main()
