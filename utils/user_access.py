from datetime import datetime
import logging

logger = logging.getLogger(__name__)

async def check_user_access(telegram_id, db_pool):
    """Check user access mode and return appropriate org_id"""
    try:
        async with db_pool.acquire() as conn:
            user_data = await conn.fetchrow(
                """SELECT 
                    u.recovery_mode, 
                    u.recovery_org_id, 
                    u.recovery_session_expires,
                    tw.turnkey_sub_org_id 
                   FROM users u 
                   LEFT JOIN turnkey_wallets tw ON u.telegram_id = tw.telegram_id 
                   WHERE u.telegram_id = $1 AND (tw.is_active = TRUE OR tw.is_active IS NULL)""",
                telegram_id
            )
            
            if not user_data:
                return None, "No wallet found", None
            
            recovery_mode, recovery_org_id, recovery_expires, normal_org_id = user_data
            
            # Check if recovery mode is active and not expired
            if recovery_mode and recovery_org_id:
                if recovery_expires and datetime.now() > recovery_expires:
                    # Auto-disable expired recovery
                    await conn.execute(
                        """UPDATE users SET 
                           recovery_mode = FALSE, 
                           recovery_org_id = NULL, 
                           recovery_session_expires = NULL 
                           WHERE telegram_id = $1""",
                        telegram_id
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

def get_recovery_warning(access_mode):
    """Get appropriate warning message"""
    if access_mode == "recovery":
        return "⚠️ *Recovery mode active. Session expires in 1 hour.*"
    return ""
