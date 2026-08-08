"""Entry point — run with: python -m stage1_nlp (or python -m worker)."""

import asyncio

from .worker import run_worker


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
