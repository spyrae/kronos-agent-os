"""Entry point: python -m kronos"""

import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

from kronos.app import main

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    asyncio.run(main())
