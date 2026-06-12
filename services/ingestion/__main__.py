"""
Entry point for the ingestion service.

Run with:
    python -m services.ingestion
or:
    python services/ingestion/__main__.py
"""

import asyncio

from .service import run_service


def main() -> None:
    asyncio.run(run_service())


if __name__ == "__main__":
    main()
