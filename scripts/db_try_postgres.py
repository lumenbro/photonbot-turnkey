import os, json, asyncio, asyncpg
from dotenv import load_dotenv
load_dotenv()

async def main():
    params = dict(
        user='postgres',
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME','postgres'),
        host=os.getenv('DB_HOST','localhost'),
        port=int(os.getenv('DB_PORT','5434')),
    )
    try:
        conn = await asyncpg.connect(**params)
        row = await conn.fetchrow('SELECT current_user as user')
        await conn.close()
        print(json.dumps({"db_connect": True, "user": row['user']}))
    except Exception as e:
        print(json.dumps({"db_connect": False, "error": str(e)}))

asyncio.run(main())


