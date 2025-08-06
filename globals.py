import asyncio
from dotenv import load_dotenv
import os
from stellar_sdk import Server
from stellar_sdk.client.aiohttp_client import AiohttpClient
import logging

logger = logging.getLogger(__name__)

# Log the current working directory and environment before loading .env
logger.info("Current working directory: %s", os.getcwd())
logger.info("Before load_dotenv, FEE_WALLET: %s", os.getenv("FEE_WALLET"))

# Explicitly specify the path to .env and force override
env_path = os.path.join(os.getcwd(), ".env")
logger.info("Attempting to load .env from: %s", env_path)
load_dotenv(env_path, override=True)  # Force override of existing environment variables

# Log the environment after loading .env
logger.info("After load_dotenv, FEE_WALLET: %s", os.getenv("FEE_WALLET"))

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not found in .env")

async def is_founder(telegram_id, db_pool):
    async with db_pool.acquire() as conn:
        result = await conn.fetchval("SELECT telegram_id FROM founders WHERE telegram_id = $1", telegram_id)
        return result is not None

class AppContext:
    def __init__(self, db_pool, queue=None):
        self.shutdown_flag = asyncio.Event()
        self.stream_lock = asyncio.Lock()
        self.db_pool = db_pool
        self.bot = None
        self.generate_keypair = None  # New
        self.sign_transaction = None  # New
        self.load_public_key = None   # Keep for public key access
        self.dp = None
        self.tasks = []
        self.queue = queue
        self.horizon_url = "https://horizon.stellar.org"
        self.client = AiohttpClient()
        self.server = Server(self.horizon_url, client=self.client)
        self.base_fee = 300  # Default base fee in stroops
        

    async def shutdown(self):
        self.shutdown_flag.set()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.db_pool:
            await self.db_pool.close()
        if self.client:
            await self.client.close()  # Close the shared client
        print("Shutdown complete.")
