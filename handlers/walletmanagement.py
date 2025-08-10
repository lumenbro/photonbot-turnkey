from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.filters import Command
from functools import partial
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Define welcome_text and main_menu_keyboard here (copied from main_menu.py for self-containment)
welcome_text = """
Welcome to @lumenbrobot!
Trade assets on Stellar with ease.
Use the buttons below to buy, sell, check balance, or manage copy trading.
"""

main_menu_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Buy", callback_data="buy"),
     InlineKeyboardButton(text="Sell", callback_data="sell")],
    [InlineKeyboardButton(text="Check Balance", callback_data="balance"),
     InlineKeyboardButton(text="Copy Trading", callback_data="copy_trading")],
    [InlineKeyboardButton(text="Withdraw", callback_data="withdraw"),
     InlineKeyboardButton(text="Referrals", callback_data="wallets")],
    [InlineKeyboardButton(text="Add Trustline", callback_data="add_trustline"),
     InlineKeyboardButton(text="Remove Trustline", callback_data="remove_trustline")],
    [InlineKeyboardButton(text="Wallet Management", callback_data="wallet_management")],
    [InlineKeyboardButton(text="Help/FAQ", callback_data="help_faq")]
])

async def get_wallet_management_menu(telegram_id, app_context):
    async with app_context.db_pool.acquire() as conn:
        # Check if user has a Turnkey wallet
        turnkey_row = await conn.fetchrow(
            "SELECT turnkey_sub_org_id FROM turnkey_wallets WHERE telegram_id = $1 AND is_active = TRUE", 
            telegram_id
        )
        
        # Check if user is a legacy migrated user
        legacy_user = await conn.fetchrow("""
            SELECT encrypted_s_address_secret, public_key, pioneer_status, source_old_db
            FROM users WHERE telegram_id = $1 AND source_old_db IS NOT NULL
        """, telegram_id)
        
        if not turnkey_row and not legacy_user:
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="No Wallet - Register First", callback_data="ignore")]]), "No Wallet - Register First"
        
        # Check session status
        session_active = await conn.fetchval(
            "SELECT session_expiry > NOW() FROM users WHERE telegram_id = $1", telegram_id
        )
        status_icon = "🟢 Active" if session_active else "🔴 Expired (Login Needed)"
        menu_text = f"Wallet Management ({status_icon}):"

        # Build menu based on user type
        menu_buttons = []
        
        if turnkey_row:
            # Turnkey user - show Turnkey options
            sub_org_id = turnkey_row['turnkey_sub_org_id']
            email = (await conn.fetchval("SELECT user_email FROM users WHERE telegram_id = $1", telegram_id)) or "unknown@lumenbro.com"
            
            mini_app_base = "https://lumenbro.com/mini-app/index.html"
            login_url = f"{mini_app_base}?action=login&orgId={sub_org_id}&email={email}"
            recovery_url = f"{mini_app_base}?action=recover&orgId={sub_org_id}&email={email}"
            check_keys_url = mini_app_base
            
            menu_buttons.extend([
                [InlineKeyboardButton(text="Login (Establish Session)", web_app=WebAppInfo(url=login_url))],
                [InlineKeyboardButton(text="Recovery (Lost Device/Passkey)", web_app=WebAppInfo(url=recovery_url))],
                [InlineKeyboardButton(text="📤 Export Wallet Keys", callback_data="export_turnkey_wallet")],
                [InlineKeyboardButton(text="Check API Keys (Debug)", web_app=WebAppInfo(url=check_keys_url))],
                [InlineKeyboardButton(text="Logout (Clear Session)", callback_data="logout")]
            ])
        
        if legacy_user:
            # Legacy migrated user - show export option and debug tools
            pioneer_badge = "👑 Pioneer" if legacy_user['pioneer_status'] else ""
            menu_buttons.extend([
                [InlineKeyboardButton(text=f"📤 Export Legacy Wallet ({pioneer_badge})", callback_data="export_legacy_wallet")],
                [InlineKeyboardButton(text="🔄 Re-trigger Migration", callback_data="re_trigger_migration")]
            ])
            
            # Debug buttons only for specific test user (your Telegram ID)
            if telegram_id == 5014800072:  # Your test user ID
                menu_buttons.extend([
                    [InlineKeyboardButton(text="🔧 Clear Cloud Storage (Debug)", callback_data="clear_cloud_storage")]
                ])
        
        # Add back button
        menu_buttons.append([InlineKeyboardButton(text="Back to Main Menu", callback_data="main_menu")])
        
        return InlineKeyboardMarkup(inline_keyboard=menu_buttons), menu_text

async def wallet_management_menu_command(message: types.Message, app_context):
    telegram_id = message.from_user.id
    keyboard, menu_text = await get_wallet_management_menu(telegram_id, app_context)
    await message.reply(menu_text, reply_markup=keyboard)

async def process_wallet_management_callback(callback: types.CallbackQuery, app_context):
    telegram_id = callback.from_user.id
    keyboard, menu_text = await get_wallet_management_menu(telegram_id, app_context)
    await callback.message.reply(menu_text, reply_markup=keyboard)
    await callback.answer()

async def process_main_menu_callback(callback: types.CallbackQuery, app_context=None):
    # Use dynamic welcome text if app_context is available
    if app_context:
        from handlers.main_menu import get_welcome_text
        dynamic_welcome = await get_welcome_text(callback.from_user.id, app_context)
        await callback.message.reply(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")
    else:
        await callback.message.reply(welcome_text, reply_markup=main_menu_keyboard, parse_mode="Markdown")
    await callback.answer()

async def process_logout_callback(callback: types.CallbackQuery, app_context):
    """Handle logout callback from wallet management menu - comprehensive session cleanup"""
    telegram_id = callback.from_user.id
    logger.info(f"Processing logout for telegram_id: {telegram_id}")
    
    try:
        async with app_context.db_pool.acquire() as conn:
            # Step 1: Clear all session-related fields from users table (but keep email!)
            await conn.execute("""
                UPDATE users SET 
                    turnkey_session_id = NULL,
                    temp_api_public_key = NULL,
                    temp_api_private_key = NULL,
                    kms_encrypted_session_key = NULL,
                    kms_key_id = NULL,
                    session_expiry = NULL,
                    session_created_at = NULL,
                    turnkey_user_id = NULL
                    -- user_email = NULL  # Don't clear email - it's needed for recovery!
                WHERE telegram_id = $1
            """, telegram_id)
            
            # Step 2: Check if user has both legacy status AND Turnkey wallets (mixed state)
            user_check = await conn.fetchrow("""
                SELECT 
                    u.source_old_db IS NOT NULL as is_legacy,
                    tw.telegram_id IS NOT NULL as has_turnkey_wallet,
                    tw.turnkey_sub_org_id,
                    tw.public_key as turnkey_public_key
                FROM users u
                LEFT JOIN turnkey_wallets tw ON u.telegram_id = tw.telegram_id AND tw.is_active = TRUE
                WHERE u.telegram_id = $1
            """, telegram_id)
            
            mixed_state_cleared = False
            if user_check and user_check['is_legacy'] and user_check['has_turnkey_wallet']:
                # User has both legacy status AND Turnkey wallet - this is the mixed state that causes issues
                logger.info(f"User {telegram_id} has mixed state (legacy + Turnkey wallet), ensuring clean logout")
                mixed_state_cleared = True
                
                # For mixed state users, we keep the Turnkey wallet but ensure no conflicting session data
                # The Turnkey wallet will be their primary wallet going forward
                
            # Step 3: Verify all session data is cleared from users table
            result = await conn.fetchrow("""
                SELECT turnkey_session_id, temp_api_public_key, temp_api_private_key, 
                       kms_encrypted_session_key, kms_key_id, session_expiry, session_created_at,
                       turnkey_user_id, user_email
                FROM users WHERE telegram_id = $1
            """, telegram_id)
            
            # Step 4: Restore email if it was accidentally cleared
            if result and result['user_email'] is None:
                logger.info(f"Email was cleared for user {telegram_id}, restoring from backup")
                # Restore the email - you should replace this with the actual email
                await conn.execute("""
                    UPDATE users SET user_email = $1 WHERE telegram_id = $2
                """, "bpeterscqa@gmail.com", telegram_id)
                logger.info(f"Email restored for user {telegram_id}")
            
            if result:
                # Check if all session fields are cleared
                session_fields = [
                    'turnkey_session_id', 'temp_api_public_key', 'temp_api_private_key',
                    'kms_encrypted_session_key', 'kms_key_id', 'session_expiry', 'session_created_at',
                    'turnkey_user_id', 'user_email'
                ]
                session_cleared = all(result[field] is None for field in session_fields)
                
                if session_cleared:
                    success_msg = "✅ Session cleared successfully."
                    if mixed_state_cleared:
                        success_msg += "\n🔧 Mixed state resolved - you'll now use your Turnkey wallet."
                    success_msg += "\n\nYou can now use /login to establish a new session."
                    await callback.message.reply(success_msg)
                    logger.info(f"✅ Complete logout successful for user {telegram_id} (mixed_state: {mixed_state_cleared})")
                else:
                    await callback.message.reply("⚠️ Session partially cleared. Some fields may still exist.")
                    logger.warning(f"⚠️ Partial logout for user {telegram_id}")
            else:
                await callback.message.reply("❌ User not found in database.")
                logger.error(f"❌ User {telegram_id} not found during logout")
                
    except Exception as e:
        logger.error(f"Error during logout for telegram_id {telegram_id}: {str(e)}")
        await callback.message.reply("❌ Error clearing session. Please try again.")
    
    await callback.answer()

async def process_turnkey_wallet_export(callback: types.CallbackQuery, app_context):
    """Handle Turnkey wallet export from wallet management menu"""
    telegram_id = callback.from_user.id
    logger.info(f"Processing Turnkey wallet export from menu for user {telegram_id}")
    
    try:
        # Get user's Turnkey wallet data and email
        async with app_context.db_pool.acquire() as conn:
            wallet_data = await conn.fetchrow("""
                SELECT tw.turnkey_sub_org_id, tw.public_key, u.user_email
                FROM turnkey_wallets tw
                LEFT JOIN users u ON tw.telegram_id = u.telegram_id
                WHERE tw.telegram_id = $1 AND tw.is_active = TRUE
            """, telegram_id)
            
            if not wallet_data:
                await callback.message.reply("❌ No active Turnkey wallet found for export.")
                await callback.answer()
                return
            
            sub_org_id = wallet_data['turnkey_sub_org_id']
            public_key = wallet_data['public_key']
            email = wallet_data['user_email'] or "unknown@lumenbro.com"
            
            # Create export URL for mini-app
            mini_app_base = "https://lumenbro.com/mini-app/index.html"
            export_url = f"{mini_app_base}?action=export&orgId={sub_org_id}&email={email}"
            
            # Create inline keyboard for export
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="📱 Open Export Page", 
                    web_app=WebAppInfo(url=export_url)
                )],
                [InlineKeyboardButton(
                    text="❌ Cancel", 
                    callback_data="cancel_export"
                )]
            ])
            
            export_message = f"""📤 **Turnkey Wallet Export**

**Wallet Details:**
• Public Key: `{public_key}`
• Organization ID: `{sub_org_id}`
• Email: `{email}`

**🔐 What you'll get:**
• Stellar private key (hex format)
• S-address format
• Backup file download
• Compatible with all Stellar wallets

**🔒 Security:**
• Enter your password to decrypt API keys
• Export happens client-side
• Keys never leave your device
• Your Turnkey passkey protects the export

**⚠️ Important:**
• Keep your exported keys secure
• Anyone with these keys can access your funds
• Store them offline in a safe location
• This is for backup purposes only

Click the button below to open the export page:"""

            await callback.message.edit_text(
                export_message, 
                reply_markup=keyboard, 
                parse_mode="Markdown"
            )
            logger.info(f"Successfully initiated Turnkey wallet export for user {telegram_id}")
            
    except Exception as e:
        logger.error(f"Error initiating Turnkey wallet export for user {telegram_id}: {e}")
        await callback.message.reply("❌ Error opening export page. Please try again or contact support.")
    
    await callback.answer()

async def process_legacy_wallet_export(callback: types.CallbackQuery, app_context):
    """Handle legacy wallet export from wallet management menu"""
    telegram_id = callback.from_user.id
    logger.info(f"Processing legacy wallet export from menu for user {telegram_id}")
    
    try:
        # Get user's encrypted S-address secret
        async with app_context.db_pool.acquire() as conn:
            user_data = await conn.fetchrow("""
                SELECT encrypted_s_address_secret, public_key, pioneer_status, source_old_db
                FROM users WHERE telegram_id = $1 AND source_old_db IS NOT NULL
            """, telegram_id)
            
            if not user_data or not user_data['encrypted_s_address_secret']:
                await callback.message.reply("❌ No legacy wallet data found for export.")
                await callback.answer()
                return
            
            # Decrypt the S-address secret
            from services.kms_service import KMSService
            import json
            
            kms_service = KMSService()
            decrypted_json = kms_service.decrypt_s_address_secret(user_data['encrypted_s_address_secret'])
            s_address_data = json.loads(decrypted_json)
            s_address_secret = s_address_data['s_address_secret']
            
            # Create export message
            pioneer_badge = "👑 Pioneer" if user_data['pioneer_status'] else ""
            
            export_message = f"""📤 **Your Old Wallet Export**

**Wallet Details:**
• Public Key: `{user_data['public_key']}`
• S-Address Secret: `{s_address_secret}`
• Status: {pioneer_badge}
• Source: {user_data['source_old_db']}

**⚠️ SECURITY WARNING:**
• This is your private key - keep it secret!
• Anyone with this key can access your funds
• Store it securely offline

**Important Notes:**
• This is your **old wallet** that will be retired
• You need to register for a **new Turnkey wallet** to continue using the bot
• Consider transferring funds to your new Turnkey wallet for continued trading

**How to Use:**
1. Import this S-address secret into any Stellar wallet (Xbull, Lobstr, etc.)
2. You'll have full control over your funds
3. Use these funds to fund your new Turnkey wallet

**Need Help?**
Contact support if you need assistance with the export."""

            await callback.message.reply(export_message, parse_mode="Markdown")
            logger.info(f"Successfully exported legacy wallet for user {telegram_id}")
            
    except Exception as e:
        logger.error(f"Error exporting legacy wallet for user {telegram_id}: {e}")
        await callback.message.reply("❌ Error exporting wallet. Please try again or contact support.")
    
    await callback.answer()

async def process_cancel_export(callback: types.CallbackQuery, app_context):
    """Handle cancel export callback"""
    await callback.message.edit_text("❌ Export cancelled.")
    await callback.answer()

async def process_clear_cloud_storage(callback: types.CallbackQuery, app_context):
    """Clear Telegram Cloud Storage for testing"""
    telegram_id = callback.from_user.id
    logger.info(f"Clearing cloud storage for user {telegram_id}")
    
    try:
        # This is a debug function - only allow for specific test user
        if telegram_id != 5014800072:  # Your test user ID
            await callback.message.reply("❌ This debug function is only available for testing.")
            await callback.answer()
            return
        
        # First, check and restore email if needed
        async with app_context.db_pool.acquire() as conn:
            user_data = await conn.fetchrow("""
                SELECT user_email FROM users WHERE telegram_id = $1
            """, telegram_id)
            
            if user_data and user_data['user_email'] is None:
                logger.info(f"Email is NULL for user {telegram_id}, restoring...")
                await conn.execute("""
                    UPDATE users SET user_email = $1 WHERE telegram_id = $2
                """, "bpeterscqa@gmail.com", telegram_id)
                await callback.message.reply("✅ Email restored to database!")
                logger.info(f"Email restored for user {telegram_id}")
            else:
                await callback.message.reply(f"📧 Current email: {user_data['user_email'] if user_data else 'Not found'}")
        
        # Clear cloud storage by sending a message with clear instructions
        clear_message = """🔧 **Cloud Storage Cleared**

This will clear any stored API keys in Telegram's cloud storage.

**To complete the clear:**
1. Open the mini-app again
2. Look for any "Clear Storage" or similar buttons
3. Click them to clear stored data

**Or manually:**
1. Go to Telegram Settings
2. Privacy and Security
3. Clear Telegram Cloud Storage
4. Select "Clear All Data"

This will force you to re-authenticate with Turnkey."""
        
        await callback.message.reply(clear_message, parse_mode="Markdown")
        logger.info(f"Cloud storage clear instructions sent to user {telegram_id}")
        
    except Exception as e:
        logger.error(f"Error clearing cloud storage for user {telegram_id}: {e}")
        await callback.message.reply("❌ Error clearing cloud storage.")
    
    await callback.answer()

async def process_re_trigger_migration(callback: types.CallbackQuery, app_context):
    """Re-trigger migration notification for legacy users"""
    telegram_id = callback.from_user.id
    logger.info(f"Re-triggering migration notification for user {telegram_id}")
    
    try:
        from handlers.main_menu import re_trigger_migration_notification
        await re_trigger_migration_notification(callback, app_context)
        
    except Exception as e:
        logger.error(f"Error re-triggering migration for user {telegram_id}: {e}")
        await callback.message.reply("❌ Error re-triggering migration. Please try again.")
        await callback.answer()

# Placeholder for future features like import/export wallets
async def import_wallet(message: types.Message, app_context):
    await message.reply("Import Wallet feature coming soon (requires Turnkey $99/month plan).")

async def export_wallet(message: types.Message, app_context):
    await message.reply("Export Wallet feature coming soon (requires Turnkey $99/month plan).")

async def generate_new_wallet(message: types.Message, app_context):
    await message.reply("Generate New Wallet feature coming soon.")

async def select_active_wallet(message: types.Message, app_context):
    await message.reply("Select Active Wallet feature coming soon.")

def register_wallet_management_handlers(dp, app_context):
    logger.info("Registering wallet management handlers")
    
    async def menu_handler(message: types.Message):
        await wallet_management_menu_command(message, app_context)
    dp.message.register(menu_handler, Command("wallet_management_menu"))
    
    async def wallet_management_handler(callback: types.CallbackQuery):
        await process_wallet_management_callback(callback, app_context)
    dp.callback_query.register(wallet_management_handler, lambda c: c.data == "wallet_management")
    
    async def main_menu_handler(callback: types.CallbackQuery):
        await process_main_menu_callback(callback, app_context)
    dp.callback_query.register(main_menu_handler, lambda c: c.data == "main_menu")
    
    async def logout_handler(callback: types.CallbackQuery):
        await process_logout_callback(callback, app_context)
    dp.callback_query.register(logout_handler, lambda c: c.data == "logout")
    
    async def legacy_export_handler(callback: types.CallbackQuery):
        await process_legacy_wallet_export(callback, app_context)
    dp.callback_query.register(legacy_export_handler, lambda c: c.data == "export_legacy_wallet")
    
    async def turnkey_export_handler(callback: types.CallbackQuery):
        await process_turnkey_wallet_export(callback, app_context)
    dp.callback_query.register(turnkey_export_handler, lambda c: c.data == "export_turnkey_wallet")
    
    async def cancel_export_handler(callback: types.CallbackQuery):
        await process_cancel_export(callback, app_context)
    dp.callback_query.register(cancel_export_handler, lambda c: c.data == "cancel_export")
    
    async def clear_storage_handler(callback: types.CallbackQuery):
        await process_clear_cloud_storage(callback, app_context)
    dp.callback_query.register(clear_storage_handler, lambda c: c.data == "clear_cloud_storage")

    async def re_trigger_migration_handler(callback: types.CallbackQuery):
        await process_re_trigger_migration(callback, app_context)
    dp.callback_query.register(re_trigger_migration_handler, lambda c: c.data == "re_trigger_migration")
    
    # Placeholder registrations for future features
    dp.message.register(lambda m: import_wallet(m, app_context), Command("import_wallet"))
    dp.message.register(lambda m: export_wallet(m, app_context), Command("export_wallet"))
    dp.message.register(lambda m: generate_new_wallet(m, app_context), Command("generate_new_wallet"))
    dp.message.register(lambda m: select_active_wallet(m, app_context), Command("select_active_wallet"))
    
    logger.info("Wallet management handlers registered successfully")
