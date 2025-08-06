from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.filters import Command
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
        row = await conn.fetchrow(
            "SELECT turnkey_sub_org_id FROM turnkey_wallets WHERE telegram_id = $1 AND is_active = TRUE", 
            telegram_id
        )
        if not row:
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="No Wallet - Register First", callback_data="ignore")]]), "No Wallet - Register First"
        sub_org_id = row['turnkey_sub_org_id']
        # Assuming email is fetched from users table if added; fallback for now
        email = (await conn.fetchval("SELECT user_email FROM users WHERE telegram_id = $1", telegram_id)) or "unknown@lumenbro.com"

        # Check session status
        session_active = await conn.fetchval(
            "SELECT session_expiry > NOW() FROM users WHERE telegram_id = $1", telegram_id
        )
        status_icon = "🟢 Active" if session_active else "🔴 Expired (Login Needed)"
        menu_text = f"Wallet Management ({status_icon}):"

    mini_app_base = "https://lumenbro.com/mini-app/index.html"
    login_url = f"{mini_app_base}?action=login&orgId={sub_org_id}&email={email}"
    recovery_url = f"{mini_app_base}?action=recover&orgId={sub_org_id}&email={email}"
    check_keys_url = mini_app_base  # Plain URL to load Mini App UI without auto-action

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Login (Establish Session)", web_app=WebAppInfo(url=login_url))],
        [InlineKeyboardButton(text="Recovery (Lost Device/Passkey)", web_app=WebAppInfo(url=recovery_url))],
        [InlineKeyboardButton(text="Check API Keys (Debug)", web_app=WebAppInfo(url=check_keys_url))],  # New button
        [InlineKeyboardButton(text="Logout (Clear Session)", callback_data="logout")],  # New logout button
        [InlineKeyboardButton(text="Back to Main Menu", callback_data="main_menu")]
    ]), menu_text

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
    
    dp.callback_query.register(lambda c: process_wallet_management_callback(c, app_context), lambda c: c.data == "wallet_management")
    dp.callback_query.register(lambda c: process_main_menu_callback(c), lambda c: c.data == "main_menu")
    dp.callback_query.register(lambda c: process_logout_callback(c, app_context), lambda c: c.data == "logout")
    
    # Placeholder registrations for future features
    dp.message.register(lambda m: import_wallet(m, app_context), Command("import_wallet"))
    dp.message.register(lambda m: export_wallet(m, app_context), Command("export_wallet"))
    dp.message.register(lambda m: generate_new_wallet(m, app_context), Command("generate_new_wallet"))
    dp.message.register(lambda m: select_active_wallet(m, app_context), Command("select_active_wallet"))
    
    logger.info("Wallet management handlers registered successfully")
