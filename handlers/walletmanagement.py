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
                [InlineKeyboardButton(text="Check API Keys (Debug)", web_app=WebAppInfo(url=check_keys_url))],
                [InlineKeyboardButton(text="Logout (Clear Session)", callback_data="logout")]
            ])
        
        if legacy_user:
            # Legacy migrated user - show export option and debug tools
            pioneer_badge = "👑 Pioneer" if legacy_user['pioneer_status'] else ""
            menu_buttons.extend([
                [InlineKeyboardButton(text=f"📤 Export Legacy Wallet ({pioneer_badge})", callback_data="export_legacy_wallet")]
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

async def process_main_menu_callback(callback: types.CallbackQuery):
    await callback.message.reply(welcome_text, reply_markup=main_menu_keyboard, parse_mode="Markdown")
    await callback.answer()

async def process_logout_callback(callback: types.CallbackQuery, app_context):
    """Handle logout callback from wallet management menu"""
    telegram_id = callback.from_user.id
    logger.info(f"Processing logout for telegram_id: {telegram_id}")
    
    try:
        async with app_context.db_pool.acquire() as conn:
            # Clear all session-related fields
            await conn.execute("""
                UPDATE users SET 
                    turnkey_session_id = NULL,
                    temp_api_public_key = NULL,
                    temp_api_private_key = NULL,
                    kms_encrypted_session_key = NULL,
                    kms_key_id = NULL,
                    session_expiry = NULL,
                    session_created_at = NULL
                WHERE telegram_id = $1
            """, telegram_id)
            
            # Verify the update
            result = await conn.fetchrow("""
                SELECT turnkey_session_id, temp_api_public_key, temp_api_private_key, 
                       kms_encrypted_session_key, kms_key_id, session_expiry, session_created_at
                FROM users WHERE telegram_id = $1
            """, telegram_id)
            
            if result:
                # Check if all session fields are cleared
                session_cleared = all(
                    result[field] is None for field in [
                        'turnkey_session_id', 'temp_api_public_key', 'temp_api_private_key',
                        'kms_encrypted_session_key', 'kms_key_id', 'session_expiry', 'session_created_at'
                    ]
                )
                
                if session_cleared:
                    await callback.message.reply("✅ Session cleared successfully. You can now use /login to establish a new session.")
                else:
                    await callback.message.reply("⚠️ Session partially cleared. Some fields may still exist.")
            else:
                await callback.message.reply("❌ User not found in database.")
                
    except Exception as e:
        logger.error(f"Error during logout for telegram_id {telegram_id}: {str(e)}")
        await callback.message.reply("❌ Error clearing session. Please try again.")
    
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

async def process_clear_cloud_storage(callback: types.CallbackQuery, app_context):
    """Handle clearing cloud storage for legacy users (debug function)"""
    telegram_id = callback.from_user.id
    logger.info(f"Processing clear cloud storage for user {telegram_id}")
    
    try:
        # Create mini-app URL that will show the clear storage button
        mini_app_base = "https://lumenbro.com/mini-app/index.html"
        clear_storage_url = f"{mini_app_base}?action=clear&telegram_id={telegram_id}"
        
        # Create inline keyboard with the clear storage link
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔧 Clear Cloud Storage", web_app=WebAppInfo(url=clear_storage_url))],
            [InlineKeyboardButton(text="Back to Wallet Management", callback_data="wallet_management")]
        ])
        
        await callback.message.reply(
            "🔧 **Clear Cloud Storage (Debug)**\n\n"
            "This will clear any existing API keys from Telegram Cloud Storage.\n"
            "Use this if you're having registration issues.\n\n"
            "Click the button below to open the mini-app and clear storage:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error creating clear storage link for user {telegram_id}: {e}")
        await callback.message.reply("❌ Error creating clear storage link. Please try again.")
    
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
    
    dp.callback_query.register(process_main_menu_callback, lambda c: c.data == "main_menu")
    
    async def logout_handler(callback: types.CallbackQuery):
        await process_logout_callback(callback, app_context)
    dp.callback_query.register(logout_handler, lambda c: c.data == "logout")
    
    async def legacy_export_handler(callback: types.CallbackQuery):
        await process_legacy_wallet_export(callback, app_context)
    dp.callback_query.register(legacy_export_handler, lambda c: c.data == "export_legacy_wallet")
    
    async def clear_storage_handler(callback: types.CallbackQuery):
        await process_clear_cloud_storage(callback, app_context)
    dp.callback_query.register(clear_storage_handler, lambda c: c.data == "clear_cloud_storage")
    
    # Placeholder registrations for future features
    dp.message.register(lambda m: import_wallet(m, app_context), Command("import_wallet"))
    dp.message.register(lambda m: export_wallet(m, app_context), Command("export_wallet"))
    dp.message.register(lambda m: generate_new_wallet(m, app_context), Command("generate_new_wallet"))
    dp.message.register(lambda m: select_active_wallet(m, app_context), Command("select_active_wallet"))
    
    logger.info("Wallet management handlers registered successfully")
