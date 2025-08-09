import logging
from datetime import datetime, timedelta
from aiogram import types
from aiogram.filters import Command

logger = logging.getLogger(__name__)

async def cmd_recover(message: types.Message, app_context):
    """Handle /recover <org_id> command for email recovery integration"""
    args = message.text.split()[1:]
    telegram_id = message.from_user.id
    
    if not args:
        await message.reply("""🔓 **Email Recovery Integration**

**Usage:** `/recover <organization_id>`

**📝 Step-by-Step Recovery:**
1️⃣ Visit: https://lumenbro.com/recovery
2️⃣ Enter your email address
3️⃣ Check email → Enter OTP code
4️⃣ Copy the organization ID shown
5️⃣ Return here: `/recover <paste_org_id>`

**⏰ Result:** 1-hour emergency wallet access
**🔑 Next Step:** Create new permanent keys on recovery page

**💡 Tip:** On mobile? Use copy/paste or ask someone to help type the ID

Need help? Contact @lumenbrobot support""", parse_mode='Markdown')
        return
    
    org_id = args[0].strip()
    
    try:
        # Validate org_id format (UUID)
        if len(org_id) != 36 or org_id.count('-') != 4:
            await message.reply("❌ Invalid organization ID format. Please check and try again.")
            return
        
        async with app_context.db_pool.acquire() as conn:
            # Check if this org belongs to this user
            user_check = await conn.fetchrow(
                "SELECT telegram_id FROM turnkey_wallets WHERE turnkey_sub_org_id = $1 AND is_active = TRUE",
                org_id
            )
            
            if not user_check:
                await message.reply("❌ No wallet found for this organization ID.")
                return
                
            if user_check['telegram_id'] != telegram_id:
                await message.reply("❌ This organization ID doesn't belong to your account.")
                return
            
            # Enable recovery session mode
            expiry_time = datetime.now() + timedelta(hours=1)
            await conn.execute(
                """UPDATE users SET 
                   recovery_mode = TRUE, 
                   recovery_org_id = $1, 
                   recovery_session_expires = $2 
                   WHERE telegram_id = $3""",
                org_id, expiry_time, telegram_id
            )
            
            # Get user email for reference
            user_data = await conn.fetchrow(
                "SELECT user_email FROM users WHERE telegram_id = $1",
                telegram_id
            )
            
            user_email = user_data['user_email'] if user_data and user_data['user_email'] else "unknown"
            
            await message.reply(f"""🔓 **Recovery Mode Activated** ✅

**📋 Session Details:**
• Organization: `{org_id}`
• Email: {user_email}
• Expires: {expiry_time.strftime('%Y-%m-%d %H:%M:%S')} UTC

**🛠️ What You Can Do Now:**
• `/balance` - Check wallet balance
• `/withdraw` - Send payments  
• Trading commands (buy/sell)
• `/copy_trading` - Access copy trading
• `/recovery_status` - Check time remaining
• `/disable_recovery` - Exit recovery mode

**🔐 Important Next Steps:**
1. Use your wallet normally for the next hour
2. **Before expiry:** Visit https://lumenbro.com/recovery  
3. **Create new permanent Telegram keys** for long-term access
4. Your wallet and funds will remain safe

**⏰ This session expires in 1 hour** - Don't wait to create permanent keys!

**Questions?** Contact @lumenbrobot support""", parse_mode='Markdown')
            
            logger.info(f"Recovery mode activated for user {telegram_id}, org {org_id}")
            
    except Exception as e:
        logger.error(f"Recovery command failed for {telegram_id}: {str(e)}")
        await message.reply("❌ Failed to activate recovery mode. Please try again or contact support.")

async def cmd_disable_recovery(message: types.Message, app_context):
    """Disable recovery mode"""
    telegram_id = message.from_user.id
    
    try:
        async with app_context.db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE users SET 
                   recovery_mode = FALSE, 
                   recovery_org_id = NULL, 
                   recovery_session_expires = NULL 
                   WHERE telegram_id = $1""",
                telegram_id
            )
            
            await message.reply("🔒 Recovery mode disabled. Use normal login credentials.")
            logger.info(f"Recovery mode disabled for user {telegram_id}")
            
    except Exception as e:
        logger.error(f"Disable recovery failed for {telegram_id}: {str(e)}")
        await message.reply("❌ Failed to disable recovery mode.")

async def cmd_recovery_status(message: types.Message, app_context):
    """Check recovery mode status"""
    telegram_id = message.from_user.id
    
    try:
        async with app_context.db_pool.acquire() as conn:
            user_data = await conn.fetchrow(
                """SELECT recovery_mode, recovery_org_id, recovery_session_expires 
                   FROM users WHERE telegram_id = $1""",
                telegram_id
            )
            
            if not user_data:
                await message.reply("❌ User not found.")
                return
            
            recovery_mode, recovery_org_id, recovery_expires = user_data
            
            if not recovery_mode:
                await message.reply("🔒 No active recovery session.")
                return
            
            now = datetime.now()
            if recovery_expires and now > recovery_expires:
                # Auto-disable expired recovery
                await conn.execute(
                    """UPDATE users SET 
                       recovery_mode = FALSE, 
                       recovery_org_id = NULL, 
                       recovery_session_expires = NULL 
                       WHERE telegram_id = $1""",
                    telegram_id
                )
                await message.reply("⏰ Recovery session expired and has been disabled.")
                return
            
            time_left = recovery_expires - now if recovery_expires else timedelta(0)
            minutes_left = int(time_left.total_seconds() / 60)
            
            await message.reply(f"""
🔓 **Active Recovery Session**

📋 Organization: `{recovery_org_id}`
⏰ Time remaining: {minutes_left} minutes
🔄 Mode: Recovery credentials

Use `/disable_recovery` to exit recovery mode.
            """, parse_mode='Markdown')
            
    except Exception as e:
        logger.error(f"Recovery status failed for {telegram_id}: {str(e)}")
        await message.reply("❌ Failed to check recovery status.")

async def cmd_help_recovery(message: types.Message, app_context):
    """Help command for users who might be lost"""
    await message.reply("""🆘 **Lost Access Help**

**If you can't access your wallet:**

**📧 Email Recovery (Recommended):**
1. Visit: https://lumenbro.com/recovery
2. Enter your email → Get OTP → Get org ID
3. Use: `/recover <org_id>` for 1-hour access
4. Create new permanent keys on recovery page

**🔧 Other Options:**
• `/register` - Create a new wallet (if you're new)
• `/recovery_status` - Check current recovery session
• Contact @lumenbrobot support for help

**💡 The recovery process gives you temporary access to create permanent new keys**

**🔐 Your funds are always safe** - even if you lose access, your wallet exists on the Stellar network""", parse_mode='Markdown')

def register_recovery_handlers(dp, app_context):
    """Register recovery command handlers"""
    
    @dp.message(Command(commands=["recover"]))
    async def handle_recover(message: types.Message):
        await cmd_recover(message, app_context)
    
    @dp.message(Command(commands=["disable_recovery"]))
    async def handle_disable_recovery(message: types.Message):
        await cmd_disable_recovery(message, app_context)
    
    @dp.message(Command(commands=["recovery_status"]))
    async def handle_recovery_status(message: types.Message):
        await cmd_recovery_status(message, app_context)
    
    # Help commands users might naturally try
    @dp.message(Command(commands=["help_recovery", "lost", "locked_out", "forgot"]))
    async def handle_help_recovery(message: types.Message):
        await cmd_help_recovery(message, app_context)
