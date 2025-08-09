# Python Telegram Bot Integration Guide

## Complete Email Recovery + New Telegram Keys Flow

This guide provides step-by-step instructions for integrating the email recovery system with your Python Telegram bot.

## 🎯 Overview

**Complete Recovery Flow:**
1. User loses Telegram access/password → Uses email recovery
2. Gets temporary 1-hour wallet access via OTP
3. **NEW:** Can create new Telegram login keys using recovery credentials
4. Integrates with Telegram bot via `/recover` command
5. Full access restored without losing wallet/sub-org

## 📋 Database Schema Updates

Add these columns to your users table:

```sql
-- Add recovery support columns
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_mode BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_org_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_session_expires TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_api_key_id TEXT;
```

## 🔧 Python Code Implementation

### 1. Recovery Command Handler

Create `commands/recovery.py`:

```python
import logging
from datetime import datetime, timedelta
from database import execute_query
from config import bot

logger = logging.getLogger(__name__)

def register_recovery_commands(bot):
    
    @bot.message_handler(commands=['recover'])
    def cmd_recover(message):
        """Handle /recover <org_id> command for email recovery integration"""
        args = message.text.split()[1:]
        telegram_id = message.from_user.id
        
        if not args:
            bot.reply_to(message, """
🔓 **Email Recovery Integration**

Usage: `/recover <organization_id>`

Get your organization ID from:
• Email recovery page: https://lumenbro.com/recovery
• Complete email OTP verification
• Copy the organization ID shown

This enables 1-hour recovery mode for your wallet.
            """, parse_mode='Markdown')
            return
        
        org_id = args[0].strip()
        
        try:
            # Validate org_id format (UUID)
            if len(org_id) != 36 or org_id.count('-') != 4:
                bot.reply_to(message, "❌ Invalid organization ID format. Please check and try again.")
                return
            
            # Check if this org belongs to this user
            user_check = execute_query(
                "SELECT telegram_id FROM turnkey_wallets WHERE turnkey_sub_org_id = %s AND is_active = TRUE",
                (org_id,)
            )
            
            if not user_check:
                bot.reply_to(message, "❌ No wallet found for this organization ID.")
                return
                
            if user_check[0][0] != telegram_id:
                bot.reply_to(message, "❌ This organization ID doesn't belong to your account.")
                return
            
            # Enable recovery session mode
            expiry_time = datetime.now() + timedelta(hours=1)
            execute_query(
                """UPDATE users SET 
                   recovery_mode = TRUE, 
                   recovery_org_id = %s, 
                   recovery_session_expires = %s 
                   WHERE telegram_id = %s""",
                (org_id, expiry_time, telegram_id)
            )
            
            # Get user email for reference
            user_data = execute_query(
                "SELECT user_email FROM users WHERE telegram_id = %s",
                (telegram_id,)
            )
            
            user_email = user_data[0][0] if user_data else "unknown"
            
            bot.reply_to(message, f"""
🔓 **Recovery Mode Activated**

✅ Organization: `{org_id}`
📧 Email: {user_email}
⏰ Expires: {expiry_time.strftime('%Y-%m-%d %H:%M:%S')} UTC

**Available Commands:**
• `/balance` - Check wallet balance
• `/send` - Send payments (recovery session)
• `/history` - View transactions
• `/trading` - Access copy trading
• `/disable_recovery` - Exit recovery mode

⚠️ **Note:** Recovery session expires in 1 hour.
Complete key regeneration if needed: https://lumenbro.com/recovery
            """, parse_mode='Markdown')
            
            logger.info(f"Recovery mode activated for user {telegram_id}, org {org_id}")
            
        except Exception as e:
            logger.error(f"Recovery command failed for {telegram_id}: {str(e)}")
            bot.reply_to(message, "❌ Failed to activate recovery mode. Please try again or contact support.")
    
    @bot.message_handler(commands=['disable_recovery'])
    def cmd_disable_recovery(message):
        """Disable recovery mode"""
        telegram_id = message.from_user.id
        
        try:
            execute_query(
                """UPDATE users SET 
                   recovery_mode = FALSE, 
                   recovery_org_id = NULL, 
                   recovery_session_expires = NULL 
                   WHERE telegram_id = %s""",
                (telegram_id,)
            )
            
            bot.reply_to(message, "🔒 Recovery mode disabled. Use normal login credentials.")
            logger.info(f"Recovery mode disabled for user {telegram_id}")
            
        except Exception as e:
            logger.error(f"Disable recovery failed for {telegram_id}: {str(e)}")
            bot.reply_to(message, "❌ Failed to disable recovery mode.")
    
    @bot.message_handler(commands=['recovery_status'])
    def cmd_recovery_status(message):
        """Check recovery mode status"""
        telegram_id = message.from_user.id
        
        try:
            user_data = execute_query(
                """SELECT recovery_mode, recovery_org_id, recovery_session_expires 
                   FROM users WHERE telegram_id = %s""",
                (telegram_id,)
            )
            
            if not user_data:
                bot.reply_to(message, "❌ User not found.")
                return
            
            recovery_mode, recovery_org_id, recovery_expires = user_data[0]
            
            if not recovery_mode:
                bot.reply_to(message, "🔒 No active recovery session.")
                return
            
            now = datetime.now()
            if recovery_expires and now > recovery_expires:
                # Auto-disable expired recovery
                execute_query(
                    """UPDATE users SET 
                       recovery_mode = FALSE, 
                       recovery_org_id = NULL, 
                       recovery_session_expires = NULL 
                       WHERE telegram_id = %s""",
                    (telegram_id,)
                )
                bot.reply_to(message, "⏰ Recovery session expired and has been disabled.")
                return
            
            time_left = recovery_expires - now if recovery_expires else timedelta(0)
            minutes_left = int(time_left.total_seconds() / 60)
            
            bot.reply_to(message, f"""
🔓 **Active Recovery Session**

📋 Organization: `{recovery_org_id}`
⏰ Time remaining: {minutes_left} minutes
🔄 Mode: Recovery credentials

Use `/disable_recovery` to exit recovery mode.
            """, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Recovery status failed for {telegram_id}: {str(e)}")
            bot.reply_to(message, "❌ Failed to check recovery status.")
```

### 2. User Access Management

Create `utils/user_access.py`:

```python
from datetime import datetime
from database import execute_query
import logging

logger = logging.getLogger(__name__)

def check_user_access(telegram_id):
    """Check user access mode and return appropriate org_id"""
    try:
        user_data = execute_query(
            """SELECT 
                u.recovery_mode, 
                u.recovery_org_id, 
                u.recovery_session_expires,
                tw.turnkey_sub_org_id 
               FROM users u 
               LEFT JOIN turnkey_wallets tw ON u.telegram_id = tw.telegram_id 
               WHERE u.telegram_id = %s AND (tw.is_active = TRUE OR tw.is_active IS NULL)""",
            (telegram_id,)
        )
        
        if not user_data:
            return None, "No wallet found", None
        
        recovery_mode, recovery_org_id, recovery_expires, normal_org_id = user_data[0]
        
        # Check if recovery mode is active and not expired
        if recovery_mode and recovery_org_id:
            if recovery_expires and datetime.now() > recovery_expires:
                # Auto-disable expired recovery
                execute_query(
                    """UPDATE users SET 
                       recovery_mode = FALSE, 
                       recovery_org_id = NULL, 
                       recovery_session_expires = NULL 
                       WHERE telegram_id = %s""",
                    (telegram_id,)
                )
                logger.info(f"Auto-disabled expired recovery session for user {telegram_id}")
                return normal_org_id, "normal", "recovery_expired"
            else:
                return recovery_org_id, "recovery", "active"
        
        # Normal mode
        if normal_org_id:
            return normal_org_id, "normal", "active"
        else:
            return None, "No wallet found", None
    
    except Exception as e:
        logger.error(f"User access check failed for {telegram_id}: {str(e)}")
        return None, "Database error", None

def get_access_status_indicator(access_mode, access_status):
    """Get status indicator for UI"""
    if access_mode == "recovery":
        if access_status == "active":
            return "🔓 (Recovery Mode)"
        elif access_status == "recovery_expired":
            return "⏰ (Recovery Expired)"
    return ""
```

### 3. Update Existing Commands

Modify your existing trading commands in `commands/trading.py`:

```python
from utils.user_access import check_user_access, get_access_status_indicator

@bot.message_handler(commands=['balance'])
def cmd_balance(message):
    """Check wallet balance with recovery support"""
    telegram_id = message.from_user.id
    
    org_id, access_mode, access_status = check_user_access(telegram_id)
    
    if not org_id:
        bot.reply_to(message, "❌ No wallet access. Use `/register` or `/recover <org_id>`.")
        return
    
    try:
        # Your existing balance logic here
        balance = get_wallet_balance(org_id)  # Your existing function
        
        status_indicator = get_access_status_indicator(access_mode, access_status)
        
        bot.reply_to(message, f"""
💰 **Wallet Balance** {status_indicator}

Balance: {balance} XLM
Organization: `{org_id}`

{get_recovery_warning(access_mode)}
        """, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Balance check failed for {telegram_id}: {str(e)}")
        bot.reply_to(message, "❌ Failed to check balance.")

def get_recovery_warning(access_mode):
    """Get appropriate warning message"""
    if access_mode == "recovery":
        return "⚠️ *Recovery mode active. Session expires in 1 hour.*"
    return ""

@bot.message_handler(commands=['send'])
def cmd_send(message):
    """Send payment with recovery support"""
    telegram_id = message.from_user.id
    
    org_id, access_mode, access_status = check_user_access(telegram_id)
    
    if not org_id:
        bot.reply_to(message, "❌ No wallet access. Use `/register` or `/recover <org_id>`.")
        return
    
    if access_mode == "recovery":
        bot.reply_to(message, """
🔓 **Recovery Mode Payment**

⚠️ You're in recovery mode (1-hour session).
For permanent access, create new Telegram keys at:
https://lumenbro.com/recovery

Continue with payment? Reply with payment details.
        """, parse_mode='Markdown')
    
    # Your existing send logic here
    # Use org_id for the transaction
```

### 4. Main Bot Integration

In your main bot file, add the recovery commands:

```python
# main.py or bot.py
from commands.recovery import register_recovery_commands

# Register recovery commands
register_recovery_commands(bot)

# Your existing bot setup
if __name__ == "__main__":
    bot.polling()
```

## 🚀 Deployment Instructions

### 1. Update Database Schema
```sql
-- Run on your production database
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_mode BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_org_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_session_expires TIMESTAMP;
```

### 2. Deploy Node.js Changes
```bash
# On your Node.js server
git pull origin main
pm2 restart all

# Run the policy creation (one-time)
node scripts/create-recovery-api-key-policy.js
```

### 3. Deploy Python Bot Changes
```bash
# On your Python bot server
# Copy the new Python files
# Update your bot code with the integration
# Restart your bot service
```

## 📱 User Flow Examples

### Complete Password Recovery:
```
1. User: "I lost my Telegram password"
2. User: Goes to https://lumenbro.com/recovery
3. User: Enters email → Gets OTP → Verifies → Recovery successful
4. User: Clicks "Create New Telegram Keys" → Sets new password
5. User: In Telegram: /recover 92d8448a-fff5-4122-8c15-b6ef7d43e39f
6. Bot: "Recovery mode activated" 
7. User: Can now trade/check balance for 1 hour
8. User: New Telegram keys work permanently
```

### Simple Recovery (no new keys):
```
1. User: Completes email recovery (1-hour session)
2. User: /recover <org_id>
3. User: Uses trading for 1 hour
4. Session expires → back to normal login
```

## 🔧 Configuration

Add to your bot's config:

```python
# config.py
RECOVERY_SESSION_HOURS = 1
RECOVERY_WARNING_MINUTES = 10  # Warn when 10 minutes left
NODE_JS_RECOVERY_URL = "https://lumenbro.com/recovery"
```

## 🛡️ Security Notes

1. **Recovery sessions are time-limited** (1 hour)
2. **Organization ID validation** prevents unauthorized access
3. **Auto-expiry** of recovery sessions
4. **New API keys** created with recovery credentials (not root)
5. **Audit logging** of all recovery operations

This integration provides seamless email recovery while maintaining security and preserving user wallets.
