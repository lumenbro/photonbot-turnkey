import os, json, asyncio, asyncpg
from dotenv import load_dotenv
load_dotenv()

USERNAMES = [
    'botadmin', 'postgres', 'bot', 'admin', 'developer', 'brandon'
]

async def try_user(user):
    params = dict(
        user=user,
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME','postgres'),
        host=os.getenv('DB_HOST','localhost'),
        port=int(os.getenv('DB_PORT','5434')),
    )
    try:
        conn = await asyncpg.connect(**params)
        row = await conn.fetchrow('SELECT current_user as user')
        await conn.close()
        return {"user": user, "ok": True, "current_user": row['user']}
    except Exception as e:
        return {"user": user, "ok": False, "error": str(e)}

async def main():
    results = []
    for u in USERNAMES:
        results.append(await try_user(u))
    print(json.dumps(results, indent=2))

asyncio.run(main())
