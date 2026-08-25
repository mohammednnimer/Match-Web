import asyncio
import asyncpg
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher
from app.config import get_settings

password_hash = PasswordHash((BcryptHasher(),))
hashed = password_hash.hash("123")

async def main():
    s = get_settings()
    url = str(s.asyncpg_dsn).replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    
    # حذف المستخدم إن وجد أولاً لمنع التعارض
    await conn.execute("DELETE FROM users WHERE email = $1;", "mo@m.com")
    
    # إضافة المستخدم بكلمة المرور 123
    await conn.execute(
        "INSERT INTO users (name, email, password, created_at) VALUES ($1, $2, $3, NOW());",
        "Admin User",
        "mo@m.com",
        hashed
    )
    
    await conn.close()
    print("Done! Password for mo@m.com set to 123 successfully.")

if __name__ == "__main__":
    asyncio.run(main())