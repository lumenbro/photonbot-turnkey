# Improved signing logic for main.py to handle mixed legacy/new users
# This should replace lines 107-130 in main.py

# Check if user has active Turnkey wallet (priority check)
wallet_data = await conn.fetchrow(
    "SELECT turnkey_sub_org_id, turnkey_key_id, public_key FROM turnkey_wallets WHERE telegram_id = $1 AND is_active = TRUE",
    int(telegram_id)
)

if wallet_data:
    # User has active Turnkey wallet - use it (even if they're legacy)
    sub_org_id = wallet_data["turnkey_sub_org_id"]
    sign_with = wallet_data["public_key"]
    public_key = wallet_data["public_key"]
    logger.info(f"Using Turnkey wallet for user {telegram_id}: org_id={sub_org_id}")
else:
    # Fallback to legacy session (only if no Turnkey wallet)
    is_legacy = await wallet_manager.is_legacy_user(telegram_id)
    
    if is_legacy:
        # Legacy user - use users table for session data
        public_key = active_wallet
        sign_with = active_wallet
        # Get sub_org_id from users table or use default
        user_data = await conn.fetchrow(
            "SELECT turnkey_session_id FROM users WHERE telegram_id = $1",
            telegram_id
        )
        sub_org_id = user_data["turnkey_session_id"] if user_data and user_data["turnkey_session_id"] else self.turnkey_org_id
        logger.info(f"Using legacy session for user {telegram_id}: org_id={sub_org_id}")
    else:
        raise ValueError(f"No active wallet found for telegram_id {telegram_id}. Create one via Node.js backend.")
