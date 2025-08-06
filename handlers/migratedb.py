import asyncpg
import asyncio
from cryptography.fernet import Fernet

MOCK_KMS_KEY = b"32-byte-static-key-for-testing-only!"

async def migrate_db():
    pool = await asyncpg.create_pool(
        user='postgres', password='password', database='stellar_bot', host='127.0.0.1', port=5432
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT telegram_id, encryption_key FROM users")
        cipher = Fernet(MOCK_KMS_KEY)
        for row in rows:
            encrypted_data_key = cipher.encrypt(row["encryption_key"].encode()).hex()
            await conn.execute(
                "UPDATE users SET encrypted_data_key = $1 WHERE telegram_id = $2",
                encrypted_data_key, row["telegram_id"]
            )
        await conn.execute("ALTER TABLE users DROP COLUMN encryption_key")
    await pool.close()

asyncio.run(migrate_db())