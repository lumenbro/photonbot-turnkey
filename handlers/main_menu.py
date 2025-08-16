import logging
from aiogram import types
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Message
from aiogram.filters import Command, CommandStart
from core.stellar import build_and_submit_transaction, has_trustline, parse_asset, load_account_async
from stellar_sdk import Asset, PathPaymentStrictReceive, ChangeTrust, Payment, Keypair
from stellar_sdk.exceptions import NotFoundError
from handlers.copy_trading import copy_trade_menu_command
from services.streaming import StreamingService
from services.trade_services import perform_buy, perform_sell, calculate_available_xlm
from services.referrals import log_xlm_volume, calculate_referral_shares, export_unpaid_rewards, daily_payout
from globals import is_founder
import secrets
import json
import os
import asyncio
import base64
from datetime import datetime
import uuid
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from sessions import create_or_refresh_session
from functools import partial
import jwt
import time
from handlers.walletmanagement import process_wallet_management_callback, process_main_menu_callback

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

class RegisterStates(StatesGroup):
    waiting_for_email = State()

class BuySellStates(StatesGroup):
    waiting_for_asset = State()
    waiting_for_amount = State()
    buy_menu_displayed = State()
    waiting_for_custom_amount = State()
    waiting_for_save_custom_amount = State()  # New state for saving custom amounts
    sell_asset_selection = State()  # New state for sell asset selection
    sell_menu_displayed = State()   # New state for sell menu display
    waiting_for_sell_percentage = State()  # New state for custom sell percentage

class WithdrawStates(StatesGroup):
    waiting_for_asset = State()
    waiting_for_address = State()
    waiting_for_amount = State()
    waiting_for_confirmation = State()

class ReferralStates(StatesGroup):
    referral_code = State()

class TrustlineStates(StatesGroup):
    waiting_for_asset_to_add = State()
    waiting_for_asset_to_remove = State()


async def get_welcome_text(telegram_id, app_context):
    """Get enhanced welcome text with session status indicator and wallet details"""
    try:
        # Get the rich welcome message first
        rich_welcome = await generate_welcome_message(telegram_id, app_context)
        
        # Check session status
        async with app_context.db_pool.acquire() as conn:
            session_active = await conn.fetchval(
                "SELECT session_expiry > NOW() FROM users WHERE telegram_id = $1", telegram_id
            )
            
            # Session status indicator
            if session_active:
                status_text = "🟢 **Session Active** - Ready to trade!"
            else:
                status_text = "🔴 **Login Required** - Use `/login` or Wallet Management to login"
            
            # Insert session status after the welcome line but before wallet details
            lines = rich_welcome.split('\n')
            if len(lines) >= 2:
                # Insert session status after the first two lines (title + subtitle)
                lines.insert(2, f"\n{status_text}\n")
                return '\n'.join(lines)
            else:
                # Fallback if format is unexpected
                return f"{rich_welcome}\n\n{status_text}"
                
    except Exception as e:
        # Fallback to basic message if any error
        try:
            async with app_context.db_pool.acquire() as conn:
                session_active = await conn.fetchval(
                    "SELECT session_expiry > NOW() FROM users WHERE telegram_id = $1", telegram_id
                )
                status_text = "🟢 **Session Active** - Ready to trade!" if session_active else "🔴 **Login Required** - Use `/login` or Wallet Management to login"
        except:
            status_text = ""
            
        return f"""Welcome to @lumenbrobot!
Trade assets on Stellar with ease.

{status_text}

Use the buttons below to buy, sell, check balance, or manage copy trading."""

async def get_custom_amount(telegram_id: int, db_pool) -> float:
    """Get user's saved custom XLM amount from database"""
    try:
        async with db_pool.acquire() as conn:
            custom_amount = await conn.fetchval(
                "SELECT custom_amount FROM users WHERE telegram_id = $1", telegram_id
            )
            return float(custom_amount) if custom_amount else None
    except Exception as e:
        logger.error(f"Error getting custom amount for user {telegram_id}: {e}")
        return None

async def save_custom_amount(telegram_id: int, amount: float, db_pool):
    """Save user's custom XLM amount to database"""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET custom_amount = $1 WHERE telegram_id = $2", 
                amount, telegram_id
            )
            logger.info(f"Saved custom XLM amount {amount} for user {telegram_id}")
    except Exception as e:
        logger.error(f"Error saving custom amount for user {telegram_id}: {e}")

async def clear_custom_amount(telegram_id: int, db_pool):
    """Clear user's saved custom XLM amount from database"""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET custom_amount = NULL WHERE telegram_id = $1", 
                telegram_id
            )
            logger.info(f"Cleared custom XLM amount for user {telegram_id}")
    except Exception as e:
        logger.error(f"Error clearing custom amount for user {telegram_id}: {e}")

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

async def get_referral_link(telegram_id: int, bot, db_pool) -> str:
    """
    Retrieve or generate a referral link for the user.
    Returns a link in the format: https://t.me/{bot_username}?start={referral_code}
    """
    bot_info = await bot.get_me()
    bot_username = bot_info.username

    async with db_pool.acquire() as conn:
        referral_code = await conn.fetchval(
            "SELECT referral_code FROM users WHERE telegram_id = $1",
            telegram_id
        )
        if not referral_code:
            # Since we don't have access to the username here, use a random code as a fallback
            # Ideally, the referral code should always be set during registration
            referral_code = f"ref-{secrets.token_urlsafe(8)}"
            await conn.execute(
                "UPDATE users SET referral_code = $1 WHERE telegram_id = $2",
                referral_code, telegram_id
            )
    return f"https://t.me/{bot_username}?start={referral_code}"

async def generate_welcome_message(telegram_id, app_context):
    bot_info = await app_context.bot.get_me()
    bot_username = bot_info.username
    try:
        public_key = await app_context.load_public_key(telegram_id)
        # Fetch the referral link for the user
        referral_link = await get_referral_link(telegram_id, app_context.bot, app_context.db_pool)

        # Check if the user is a pioneer
        async with app_context.db_pool.acquire() as conn:
            is_pioneer = await conn.fetchval(
                "SELECT 1 FROM founders WHERE telegram_id = $1", telegram_id
            )
            # Check if the user was referred (i.e., has a referrer in the referrals table)
            is_referred = await conn.fetchval(
                "SELECT 1 FROM referrals WHERE referee_id = $1", telegram_id
            )

        pioneer_status = "\n*Status*: You are a pioneer! 🎉\n" if is_pioneer else ""
        # Add disclaimer for referred users
        referral_disclaimer = (
            "\n*Referral Discount*: You've received a 10% discount on fees because you were referred! "
            "Note: If your referrer unregisters, you may lose this discount.\n"
        ) if is_referred else ""

        try:
            account = await load_account_async(public_key, app_context)
            xlm_balance = float(next((b["balance"] for b in account["balances"] if b["asset_type"] == "native"), "0"))
            welcome_text = (
                f"*Welcome to @{bot_username}!*\n"
                f"Jump into Stellar trading with ease!\n\n"
                f"*Your Wallet:* `{public_key}`\n"
                f"*XLM Balance:* {xlm_balance:.7f}\n"
                f"{pioneer_status}"  # Add pioneer status here
                f"{referral_disclaimer}"  # Add referral disclaimer here
                f"Trade issued assets and Soroban SAC, stream copy trade wallets, and earn rewards with referrals.\n"
                f"Invite friends and earn rewards! Your referral link: `{referral_link}`\n\n"
                f"Use the buttons below to get started.\n\n"
                f"*New Users* Fund your wallet with XLM to trade. See /help for wallet and security tips.\n"
                f"*Note:* Soroban supported for copy trades!"
            )
        except NotFoundError:
            welcome_text = (
                f"*Welcome to @{bot_username}!*\n"
                f"Jump into Stellar trading with ease!\n\n"
                f"*Your Wallet:* `{public_key}`\n"
                f"*XLM Balance:* Not funded\n"
                f"{pioneer_status}"  # Add pioneer status here
                f"{referral_disclaimer}"  # Add referral disclaimer here
                f"Your wallet needs XLM to start trading. Send XLM to your public key from an exchange "
                f"(e.g., Coinbase, Kraken, Lobstr).\n\n"
                f"Trade issued assets and Soroban SAC, stream copy trade wallets, and earn rewards with referrals.\n"
                f"Invite friends and earn rewards! Your referral link: `{referral_link}`\n\n"
                f"Use the buttons below to get started. See /help for wallet and security tips.\n"
                f"*Note:* Soroban supported for copy trades!"
            )
    except Exception as e:
        logger.error(f"Error fetching wallet info for welcome message: {str(e)}", exc_info=True)
        welcome_text = (
            f"*Welcome to @{bot_username}!*\n"
            f"Jump into Stellar trading with ease!\n\n"
            f"Trade issued assets and Soroban SAC, stream copy trade wallets, and earn rewards with referrals.\n"
            f"Use the buttons below to get started.\n\n"
            f"*New Users* Fund your wallet with XLM to trade. See /help for wallet and security tips.\n"
            f"*Note:* Soroban supported for copy trades!"
        )
    return welcome_text

async def start_command(message: types.Message, app_context, streaming_service: StreamingService, state: FSMContext):
    telegram_id = message.from_user.id
    logger.info(f"Start command: from_user.id={telegram_id}, chat_id={message.chat.id}, is_group={message.chat.type == 'group'}")
    chat_id = message.chat.id

    # Log the raw message text
    text = message.text.strip()
    logger.debug(f"Raw start command text: '{text}'")

    # Parse the parameter from message.text
    parameter = None
    if text.startswith('/start'):
        parts = text.split(maxsplit=1)
        parameter = parts[1].strip().lower() if len(parts) > 1 else None
        logger.debug(f"Detected parameter from text: '{parameter}'")

    founder_signup = False
    # Check for founder-signup (to be updated to pioneer-signup)
    if parameter == 'pioneer-signup':
        founder_signup = True
        logger.info(f"Founder sign-up detected for user {telegram_id}")

    if founder_signup:
        logger.info(f"Founder sign-up detected for user {telegram_id}")
        async with app_context.db_pool.acquire() as conn:
            exists = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", telegram_id)
        if not exists and message.from_user.is_bot:
            logger.info(f"Ignoring start command from bot itself for telegram_id {telegram_id}")
            return
        elif not exists:
            # User needs to register first; redirect to registration
            await message.reply("You're not registered yet. Let's get you set up as a founder. Use /register to proceed.")
            await state.update_data(founder_signup=True)
            logger.info(f"Set founder_signup=True in state for user {telegram_id}")
            return
        else:
            # User is registered; attempt to add them as a founder
            try:
                await add_founder(telegram_id, app_context.db_pool)
                await message.reply(
                "🎉 Congratulations! You've been registered as a pioneer.\n\n"
                "Please use /start to proceed to the main menu."
                )
            except ValueError as e:
                # Handle the case where the founder limit is reached or other validation errors
                await message.reply(str(e))
            except Exception as e:
                # Handle other errors (e.g., database issues)
                await message.reply("An error occurred while registering you as a pioneer. Please try again later.")
                logger.error(f"Error adding pioneer: {str(e)}", exc_info=True)
            return  # Exit after handling pioneer sign-up

    # Existing referral logic
    if parameter and 'ref-' in parameter:
        referral_code = parameter  # Store the full referral code including 'ref-' prefix
        await state.update_data(referral_code=referral_code)
        logger.info(f"Stored referral code {referral_code} in state for user {telegram_id}")
    else:
        logger.info("No parameter or unrecognized parameter provided with /start command")

    # Check if user exists and if they are a legacy migrated user
    async with app_context.db_pool.acquire() as conn:
        user_data = await conn.fetchrow("""
            SELECT telegram_id, source_old_db, encrypted_s_address_secret, pioneer_status, 
                   migration_notified, public_key
            FROM users WHERE telegram_id = $1
        """, telegram_id)
        
    if not user_data and message.from_user.is_bot:
        logger.info(f"Ignoring start command from bot itself for telegram_id {telegram_id}")
        return
    elif not user_data:
        await message.reply("You're not registered yet. Use /register to get started.")
    else:
        # Check if this is a legacy migrated user
        if user_data['source_old_db'] and user_data['encrypted_s_address_secret']:
            # This is a legacy migrated user
            logger.info(f"Legacy migrated user detected: {telegram_id}")
            
            # Check if they've been notified about migration
            if not user_data['migration_notified']:
                # Show migration notification with export option
                await show_migration_notification(message, user_data, app_context)
            else:
                # Show normal welcome message with migration reminder
                # Add migration reminder for legacy users
                migration_reminder = "\n\n💡 **Migration Reminder:** You can export your old wallet keys or re-trigger migration options from the Wallet Management menu."
                
                dynamic_welcome = await get_welcome_text(telegram_id, app_context)
                await message.reply(dynamic_welcome + migration_reminder, reply_markup=main_menu_keyboard, parse_mode="Markdown")
        else:
            # Regular user (not migrated)
            dynamic_welcome = await get_welcome_text(telegram_id, app_context)
            await message.reply(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def show_migration_notification(message: types.Message, user_data, app_context):
    """Show migration notification to legacy users with export option"""
    telegram_id = user_data['telegram_id']
    pioneer_status = "👑 Pioneer" if user_data['pioneer_status'] else "Regular User"
    
    notification_text = f"""🔔 **Important: Your Old Wallet is Being Retired**

Hello! We've upgraded our system to be more secure. Your old wallet data has been safely migrated.

**Your Status:**
• {pioneer_status}
• Public Key: `{user_data['public_key']}`

**What You Need to Know:**
⚠️ **Your old wallet will be retired** - it won't work with the new bot
✅ Your funds are safe and accessible
📱 **You need to register for a new Turnkey wallet** to continue using the bot

**Next Steps:**
1. Export your old wallet keys (for fund management)
2. Register for a new Turnkey wallet to continue trading
3. Transfer funds from old wallet to new wallet (optional)

Would you like to export your old wallet keys now?"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Export Old Wallet Keys", callback_data="export_legacy_wallet")],
        [InlineKeyboardButton(text="📱 Register New Turnkey Wallet", callback_data="register_new_wallet")],
        [InlineKeyboardButton(text="⏰ Later", callback_data="migration_notified_later")],
        [InlineKeyboardButton(text="❓ Help", callback_data="migration_help")]
    ])
    
    await message.reply(notification_text, reply_markup=keyboard, parse_mode="Markdown")

async def process_migration_export(callback: types.CallbackQuery, app_context):
    """Handle legacy wallet export from migration notification"""
    telegram_id = callback.from_user.id
    logger.info(f"Processing legacy wallet export for user {telegram_id}")
    
    try:
        # Get user's encrypted S-address secret
        async with app_context.db_pool.acquire() as conn:
            user_data = await conn.fetchrow("""
                SELECT encrypted_s_address_secret, COALESCE(legacy_public_key, public_key) as public_key, pioneer_status
                FROM users WHERE telegram_id = $1
            """, telegram_id)
            
            if not user_data or not user_data['encrypted_s_address_secret']:
                await callback.message.reply("❌ No wallet data found for export.")
                await callback.answer()
                return
            
            # Decrypt the S-address secret
            from services.kms_service import KMSService
            
            kms_service = KMSService()
            s_address_secret = kms_service.decrypt_s_address_secret(user_data['encrypted_s_address_secret'])
            
            # Create export message
            pioneer_badge = "👑 Pioneer" if user_data['pioneer_status'] else ""
            
            export_message = f"""📤 **Your Old Wallet Export**

**Wallet Details:**
• Public Key: `{user_data['public_key']}`
• S-Address Secret: `{s_address_secret}`
• Status: {pioneer_badge}

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

            # Mark as notified
            await conn.execute("""
                UPDATE users SET migration_notified = TRUE 
                WHERE telegram_id = $1
            """, telegram_id)
            
            # Create keyboard with delete and continue options
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑️ Delete Message", callback_data="delete_export_message")],
                [InlineKeyboardButton(text="🔄 Continue to Turnkey Registration", callback_data="continue_turnkey_registration")]
            ])
            
            await callback.message.reply(export_message, parse_mode="Markdown", reply_markup=keyboard)
            logger.info(f"Successfully exported wallet for user {telegram_id}")
            
    except Exception as e:
        logger.error(f"Error exporting legacy wallet for user {telegram_id}: {e}")
        await callback.message.reply("❌ Error exporting wallet. Please try again or contact support.")
    
    await callback.answer()

async def process_migration_notified_later(callback: types.CallbackQuery, app_context):
    """Handle 'later' option for migration notification"""
    telegram_id = callback.from_user.id
    
    try:
        async with app_context.db_pool.acquire() as conn:
            # Mark as notified but allow re-triggering
            await conn.execute("""
                UPDATE users SET migration_notified = TRUE, migration_notified_at = NOW()
                WHERE telegram_id = $1
            """, telegram_id)
        
        await callback.message.reply(
            "✅ Got it! You can export your wallet keys anytime from the Wallet Management menu.\n\n"
            "Use /start to access the main menu.\n\n"
            "💡 **Tip:** If you need to see the migration options again, use the Wallet Management menu."
        )
        
    except Exception as e:
        logger.error(f"Error marking migration as notified for user {telegram_id}: {e}")
        await callback.message.reply("❌ Error. Please try again.")
    
    await callback.answer()

async def re_trigger_migration_notification(callback: types.CallbackQuery, app_context):
    """Re-trigger migration notification for legacy users"""
    telegram_id = callback.from_user.id
    logger.info(f"Re-triggering migration notification for user {telegram_id}")
    
    try:
        async with app_context.db_pool.acquire() as conn:
            # Get user data for migration notification
            user_data = await conn.fetchrow("""
                SELECT public_key, pioneer_status, encrypted_s_address_secret
                FROM users WHERE telegram_id = $1 AND source_old_db IS NOT NULL
            """, telegram_id)
            
            if not user_data:
                await callback.message.reply("❌ No legacy user data found.")
                await callback.answer()
                return
            
            # Reset migration notification flag to allow re-triggering
            await conn.execute("""
                UPDATE users SET migration_notified = FALSE, migration_notified_at = NULL
                WHERE telegram_id = $1
            """, telegram_id)
            
            # Show migration notification again
            await show_migration_notification(callback.message, {
                'telegram_id': telegram_id,
                'public_key': user_data['public_key'],
                'pioneer_status': user_data['pioneer_status']
            }, app_context)
            
            await callback.message.reply("🔄 Migration notification re-triggered!")
            
    except Exception as e:
        logger.error(f"Error re-triggering migration for user {telegram_id}: {e}")
        await callback.message.reply("❌ Error re-triggering migration. Please try again.")
    
    await callback.answer()

async def process_register_new_wallet(callback: types.CallbackQuery, app_context):
    """Handle registration for new Turnkey wallet from legacy users"""
    telegram_id = callback.from_user.id
    logger.info(f"Processing new wallet registration for legacy user {telegram_id}")
    
    try:
        # Check if user is a legacy migrated user
        async with app_context.db_pool.acquire() as conn:
            user_data = await conn.fetchrow("""
                SELECT telegram_id, source_old_db, pioneer_status, public_key
                FROM users WHERE telegram_id = $1 AND source_old_db IS NOT NULL
            """, telegram_id)
            
            if not user_data:
                await callback.message.reply("❌ You don't appear to be a legacy migrated user.")
                await callback.answer()
                return
            
            # SAFETY CHECK: Check if user already has a Turnkey wallet
            existing_wallet = await conn.fetchrow("""
                SELECT public_key, is_active FROM turnkey_wallets 
                WHERE telegram_id = $1 AND is_active = TRUE
            """, telegram_id)
            
            if existing_wallet:
                # User already has an active Turnkey wallet - prevent overwriting
                warning_message = f"""⚠️ **You Already Have a Turnkey Wallet**

**Current Active Wallet:**
• Public Key: `{existing_wallet['public_key']}`
• Status: Active

**Why You're Seeing This:**
You've already completed the migration and have a working Turnkey wallet. The migration button is for users who haven't registered yet.

**What You Can Do:**
• Use your existing wallet for trading
• Export your old wallet keys if needed (from Wallet Management)
• Contact support if you need to reset your wallet

**Need Help?**
Contact @lumenbrobot support if you need assistance."""

                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")],
                    [InlineKeyboardButton(text="📤 Export Old Wallet", callback_data="export_legacy_wallet")]
                ])
                
                await callback.message.reply(warning_message, reply_markup=keyboard, parse_mode="Markdown")
                logger.info(f"Prevented legacy user {telegram_id} from overwriting existing Turnkey wallet")
                await callback.answer()
                return
            
            # Mark as notified about migration
            await conn.execute("""
                UPDATE users SET migration_notified = TRUE 
                WHERE telegram_id = $1
            """, telegram_id)
        
        # Generate registration link for new Turnkey wallet
        mini_app_url = f"https://lumenbro.com/mini-app/index.html?action=register&legacy_user=true&telegram_id={telegram_id}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Register New Turnkey Wallet", web_app=WebAppInfo(url=mini_app_url))],
            [InlineKeyboardButton(text="⏰ Later", callback_data="migration_notified_later")]
        ])
        
        pioneer_status = "👑 Pioneer" if user_data['pioneer_status'] else "Regular User"
        registration_message = f"""📱 **Register Your New Turnkey Wallet**

**Your Legacy Status:**
• {pioneer_status} (will be preserved)
• Old Public Key: `{user_data['public_key']}`

**What You're Doing:**
• Creating a new secure Turnkey wallet
• This will be your new trading wallet
• Your old wallet will be retired

**Next Steps:**
1. Click "Register New Turnkey Wallet" below
2. Follow the registration process
3. Your new wallet will be ready for trading

**Note:** Your pioneer status will be preserved in the new system."""

        await callback.message.reply(registration_message, reply_markup=keyboard, parse_mode="Markdown")
        logger.info(f"Successfully initiated new wallet registration for legacy user {telegram_id}")
        
    except Exception as e:
        logger.error(f"Error processing new wallet registration for user {telegram_id}: {e}")
        await callback.message.reply("❌ Error processing registration. Please try again or contact support.")
    
    await callback.answer()

async def process_migration_help(callback: types.CallbackQuery):
    """Handle help request for migration"""
    help_text = """❓ **Migration Help**

**What is this migration?**
We've upgraded our system to be more secure and user-friendly. Your old wallet data has been safely transferred to the new system.

**What happens to my old wallet?**
• Your old wallet will be retired and won't work with the new bot
• You need to register for a new Turnkey wallet to continue trading
• Your funds are safe and accessible through the exported keys

**What should I do?**
1. Export your old wallet keys (for fund management)
2. Register for a new Turnkey wallet to continue trading
3. Optionally transfer funds from old wallet to new wallet

**What are wallet keys?**
• Public Key: Your wallet address (safe to share)
• S-Address Secret: Your private key (keep secret!)

**Why export my keys?**
• Full control over your funds
• Access from any Stellar wallet
• Backup in case of bot issues

**Is this safe?**
✅ Your funds are secure
✅ The export is encrypted
✅ You control your private key

**Need more help?**
Contact @lumenbrobot support in Telegram."""

    await callback.message.reply(help_text, parse_mode="Markdown")
    await callback.answer()

async def delete_export_message(callback: types.CallbackQuery):
    """Delete the message containing sensitive S-address secrets"""
    try:
        # Delete the message that contains the sensitive data
        await callback.message.delete()
        await callback.answer("✅ Message deleted for security")
        logger.info(f"User {callback.from_user.id} deleted export message")
    except Exception as e:
        logger.error(f"Error deleting export message for user {callback.from_user.id}: {e}")
        await callback.answer("❌ Could not delete message")

async def continue_turnkey_registration(callback: types.CallbackQuery, app_context):
    """Continue to Turnkey wallet registration after export"""
    telegram_id = callback.from_user.id
    logger.info(f"Continuing to Turnkey registration for user {telegram_id}")
    
    try:
        # Check if user is a legacy migrated user
        async with app_context.db_pool.acquire() as conn:
            user_data = await conn.fetchrow("""
                SELECT telegram_id, source_old_db, pioneer_status, public_key
                FROM users WHERE telegram_id = $1 AND source_old_db IS NOT NULL
            """, telegram_id)
            
            if not user_data:
                await callback.message.reply("❌ You don't appear to be a legacy migrated user.")
                await callback.answer()
                return
            
            # SAFETY CHECK: Check if user already has a Turnkey wallet
            existing_wallet = await conn.fetchrow("""
                SELECT public_key, is_active FROM turnkey_wallets 
                WHERE telegram_id = $1 AND is_active = TRUE
            """, telegram_id)
            
            if existing_wallet:
                # User already has an active Turnkey wallet - prevent overwriting
                warning_message = f"""⚠️ **You Already Have a Turnkey Wallet**

**Current Active Wallet:**
• Public Key: `{existing_wallet['public_key']}`
• Status: Active

**Why You're Seeing This:**
You've already completed the migration and have a working Turnkey wallet. The migration button is for users who haven't registered yet.

**What You Can Do:**
• Use your existing wallet for trading
• Export your old wallet keys if needed (from Wallet Management)
• Contact support if you need to reset your wallet

**Need Help?**
Contact @lumenbrobot support if you need assistance."""

                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")],
                    [InlineKeyboardButton(text="📤 Export Old Wallet", callback_data="export_legacy_wallet")]
                ])
                
                await callback.message.reply(warning_message, reply_markup=keyboard, parse_mode="Markdown")
                logger.info(f"Prevented legacy user {telegram_id} from overwriting existing Turnkey wallet")
                await callback.answer()
                return
        
        # Generate registration link for new Turnkey wallet
        mini_app_url = f"https://lumenbro.com/mini-app/index.html?action=register&legacy_user=true&telegram_id={telegram_id}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Register New Turnkey Wallet", web_app=WebAppInfo(url=mini_app_url))],
            [InlineKeyboardButton(text="⏰ Later", callback_data="migration_notified_later")]
        ])
        
        pioneer_status = "👑 Pioneer" if user_data['pioneer_status'] else "Regular User"
        registration_message = f"""📱 **Register Your New Turnkey Wallet**

**Your Legacy Status:**
• {pioneer_status} (will be preserved)
• Old Public Key: `{user_data['public_key']}`

**What You're Doing:**
• Creating a new secure Turnkey wallet
• This will be your new trading wallet
• Your old wallet will be retired

**Next Steps:**
1. Click "Register New Turnkey Wallet" below
2. Follow the registration process
3. Your new wallet will be ready for trading

**Note:** Your pioneer status will be preserved in the new system."""

        await callback.message.reply(registration_message, reply_markup=keyboard, parse_mode="Markdown")
        logger.info(f"Successfully continued to Turnkey registration for user {telegram_id}")
        
    except Exception as e:
        logger.error(f"Error continuing to Turnkey registration for user {telegram_id}: {e}")
        await callback.message.reply("❌ Error processing registration. Please try again or contact support.")
    
    await callback.answer()

async def cancel_command(message: types.Message, state: FSMContext):
    await state.clear()
    await message.reply("Action cancelled. Use /start to begin again.")

async def get_founder_count(db_pool):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM founders")

async def check_pioneer_eligibility(telegram_id, db_pool):
    """Check if user is eligible to become a pioneer"""
    try:
        import aiohttp
        
        # Call Node.js endpoint to check eligibility
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://lumenbro.com/api/check-pioneer-eligibility?telegramId={telegram_id}"
            ) as response:
                if response.status != 200:
                    # Fallback to local check
                    return await check_pioneer_eligibility_local(telegram_id, db_pool)
                
                eligibility_data = await response.json()
                return eligibility_data
                
    except Exception as e:
        logger.error(f"Error checking pioneer eligibility: {e}")
        # Fallback to local check
        return await check_pioneer_eligibility_local(telegram_id, db_pool)

async def check_pioneer_eligibility_local(telegram_id, db_pool):
    """Local fallback for checking pioneer eligibility"""
    async with db_pool.acquire() as conn:
        # Check if user was referred (referees cannot become pioneers)
        referral_exists = await conn.fetchval(
            "SELECT 1 FROM referrals WHERE referee_id = $1", telegram_id
        )
        
        if referral_exists:
            return { 'eligible': False, 'reason': "Users who were referred cannot become pioneers." }

        # Check current pioneer count
        founder_count = await conn.fetchval("SELECT COUNT(*) FROM founders")
        
        if founder_count >= 25:
            return { 'eligible': False, 'reason': "Sorry, the pioneer program is full! Only 25 slots are available." }

        return { 'eligible': True, 'currentCount': founder_count }

async def add_founder(telegram_id, db_pool):
    """Add user as pioneer (founder) using Node.js endpoint for consistency"""
    try:
        import aiohttp
        import json
        
        # Call Node.js endpoint to check eligibility and add pioneer
        async with aiohttp.ClientSession() as session:
            # First check eligibility
            async with session.get(
                f"https://lumenbro.com/api/check-pioneer-eligibility?telegramId={telegram_id}"
            ) as response:
                if response.status != 200:
                    raise ValueError("Error checking pioneer eligibility")
                
                eligibility_data = await response.json()
                
                if not eligibility_data.get('eligible', False):
                    raise ValueError(eligibility_data.get('reason', 'Unknown error'))
            
            # If eligible, register as pioneer
            async with session.post(
                "https://lumenbro.com/api/register-pioneer",
                json={'telegramId': telegram_id}
            ) as response:
                if response.status != 200:
                    raise ValueError("Error registering as pioneer")
                
                result = await response.json()
                if not result.get('success', False):
                    raise ValueError(result.get('message', 'Unknown error'))
        
        return True
        
    except aiohttp.ClientError as e:
        logger.error(f"Network error calling Node.js endpoint: {e}")
        # Fallback to local check if Node.js is unavailable
        return await add_founder_local(telegram_id, db_pool)
    except Exception as e:
        logger.error(f"Error in add_founder: {e}")
        raise ValueError(str(e))

async def add_founder_local(telegram_id, db_pool):
    """Local fallback for adding founder (original implementation)"""
    async with db_pool.acquire() as conn:
        # Check if the user was referred (is a referee)
        referral_exists = await conn.fetchval(
            "SELECT 1 FROM referrals WHERE referee_id = $1", telegram_id
        )
        if referral_exists:
            raise ValueError("Users who were referred cannot become pioneers. Click /start to proceed.")

        # Check the current number of founders (for user-friendly error handling)
        founder_count = await conn.fetchval("SELECT COUNT(*) FROM founders")
        if founder_count >= 25:
            raise ValueError("Sorry, the founder program is full! Only 25 slots are available.")

        # Ensure the user exists in the users table
        user_exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE telegram_id = $1", telegram_id
        )
        if not user_exists:
            # Add the user with a placeholder referral code and public key
            await conn.execute(
                "INSERT INTO users (telegram_id, referral_code, public_key) VALUES ($1, $2, $3)",
                telegram_id, f"FOUNDER_{telegram_id}", f"PUBLIC_KEY_{telegram_id}"
            )

        # Attempt the insert into founders (the database trigger will enforce the limit)
        try:
            await conn.execute(
                "INSERT INTO founders (telegram_id) VALUES ($1) ON CONFLICT (telegram_id) DO NOTHING",
                telegram_id
            )
        except Exception as e:
            if "Cannot add more founders" in str(e):
                raise ValueError("Sorry, the founder program is full! Only 25 slots are available.")
            raise  # Re-raise other errors

def canonicalize_json(obj):
    """Canonicalize JSON per RFC 8785."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))

async def register_command(message: types.Message, app_context, state: FSMContext):
    telegram_id = message.from_user.id
    username = message.from_user.username
    logger.info(f"Register command: from_user.id={telegram_id}, chat_id={message.chat.id}, is_group={message.chat.type == 'group'}")
    chat_id = message.chat.id

    # Fetch username if None
    if not username:
        try:
            chat = await app_context.bot.get_chat(telegram_id)
            username = chat.username
            logger.info(f"Fetched username for {telegram_id}: {username}")
        except Exception as e:
            logger.error(f"Failed to fetch username for {telegram_id}: {str(e)}")
            username = None

    # Check if user exists and if they are a legacy migrated user
    async with app_context.db_pool.acquire() as conn:
        user_data = await conn.fetchrow("""
            SELECT telegram_id, source_old_db, pioneer_status, referral_code
            FROM users WHERE telegram_id = $1
        """, telegram_id)
        
        if user_data:
            # Check if this is a legacy migrated user
            if user_data['source_old_db']:
                # Legacy user - skip referral code requirement and use existing data
                logger.info(f"Legacy user {telegram_id} registering for new Turnkey wallet")
                
                # Get referrer_id from referrals table if user was referred
                referrer_id = await conn.fetchval(
                    "SELECT referrer_id FROM referrals WHERE referee_id = $1", telegram_id
                )
                referral_code = user_data['referral_code']
                
                # Generate JWT token for legacy user
                jwt_secret = os.getenv('JWT_SECRET')
                token = jwt.encode({
                    'telegram_id': telegram_id,
                    'referrer_id': referrer_id,
                    'legacy_user': True,
                    'pioneer_status': user_data['pioneer_status'],
                    'exp': time.time() + 600  # 10min expiry
                }, jwt_secret, algorithm='HS256')

                # Send link button for legacy user
                mini_app_url = f"https://lumenbro.com/mini-app/index.html?action=register&legacy_user=true&telegram_id={telegram_id}&referrer_id={referrer_id or ''}"
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📱 Register New Turnkey Wallet", web_app=WebAppInfo(url=mini_app_url))]
                ])
                
                pioneer_status = "👑 Pioneer" if user_data['pioneer_status'] else "Regular User"
                await message.reply(
                    f"📱 **Register Your New Turnkey Wallet**\n\n"
                    f"**Your Legacy Status:** {pioneer_status} (will be preserved)\n\n"
                    f"**What You're Doing:**\n"
                    f"• Creating a new secure Turnkey wallet\n"
                    f"• This will be your new trading wallet\n"
                    f"• Your old wallet will be retired\n\n"
                    f"**Next Steps:**\n"
                    f"1. Click 'Register New Turnkey Wallet' below\n"
                    f"2. Follow the registration process\n"
                    f"3. Your new wallet will be ready for trading\n\n"
                    f"**Note:** Your pioneer status will be preserved in the new system.",
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
                await state.clear()
                return
            else:
                # Regular existing user
                await message.reply("You're already registered!")
                return

    # Handle referral code for new users
    referral_code = None
    referrer_id = None
    if state:
        data = await state.get_data()
        referral_code = data.get('referral_code')

    if not referral_code:
        text = message.text.strip()
        if text.startswith('/start') or text.startswith('/register'):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                args = parts[1]
                if 'ref-' in args:
                    referral_code = args  # Keep the full code including 'ref-' prefix
                else:
                    referral_code = args.strip()

    if not referral_code:
        await message.reply("Do you have a referral code? Enter it (e.g., ref-tgusername) or reply 'none'.")
        await state.set_state(ReferralStates.referral_code)
        return

    if referral_code and referral_code.lower() != 'none':
        async with app_context.db_pool.acquire() as conn:
            referrer_id = await conn.fetchval(
                "SELECT telegram_id FROM users WHERE LOWER(referral_code) = LOWER($1)",
                referral_code
            )
        if referrer_id:
            logger.info(f"Found referrer {referrer_id} for referral_code {referral_code}")
        else:
            logger.warning(f"No referrer found for {referral_code}")
            await message.reply("Invalid referral code. Proceeding without a referrer.")

    bot_id = app_context.bot.id
    if telegram_id == bot_id:
        logger.error(f"Attempted registration with bot ID {telegram_id}")
        await message.reply("Bot cannot register itself!")
        await state.clear()
        return

    # Generate JWT token for new user
    jwt_secret = os.getenv('JWT_SECRET')  # Set in .env
    token = jwt.encode({
        'telegram_id': telegram_id,
        'referrer_id': referrer_id,
        'exp': time.time() + 600  # 10min expiry
    }, jwt_secret, algorithm='HS256')

    # In TEST_MODE, bypass mini-app and register local test wallet
    if app_context.is_test_mode:
        try:
            from stellar_sdk import Keypair
            test_secret = os.getenv('TEST_SIGNER_SECRET')
            if not test_secret:
                await message.reply("TEST_MODE detected but TEST_SIGNER_SECRET is missing.")
            else:
                test_public = Keypair.from_secret(test_secret).public_key
                async with app_context.db_pool.acquire() as conn:
                    exists = await conn.fetchval("SELECT 1 FROM users WHERE telegram_id = $1", telegram_id)
                    if not exists:
                        await conn.execute(
                            "INSERT INTO users (telegram_id, public_key, referral_code) VALUES ($1, $2, $3)",
                            telegram_id, test_public, f"ref-api-{telegram_id}"
                        )
                    else:
                        await conn.execute(
                            "UPDATE users SET public_key = $1 WHERE telegram_id = $2",
                            test_public, telegram_id
                        )
                await message.reply("Test mode: wallet registered locally. You can proceed without the mini-app.")
        except Exception as e:
            await message.reply(f"Test mode registration failed: {str(e)}")
        await state.clear()
        return

    # Send link button (to lumenbro.com in production)
    mini_app_url = f"https://lumenbro.com/mini-app/index.html?action=register&referrer_id={referrer_id or ''}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Link Telegram to Turnkey", web_app=WebAppInfo(url=mini_app_url))]
    ])
    await message.reply("Open Mini App to save Turnkey API keys securely to Telegram Cloud:", reply_markup=keyboard)
    await state.clear()

async def process_referral_code(message: types.Message, state: FSMContext, app_context):
    referral_code = message.text.strip()
    telegram_id = message.from_user.id
    username = message.from_user.username
    chat_id = message.chat.id

    # Fetch username if None
    if not username:
        try:
            chat = await app_context.bot.get_chat(telegram_id)
            username = chat.username
            logger.info(f"Fetched username for {telegram_id}: {username}")
        except Exception as e:
            logger.error(f"Failed to fetch username for {telegram_id}: {str(e)}")
            username = None

    if referral_code.lower() == 'none':
        referral_code = None

    referrer_id = None
    if referral_code:
        async with app_context.db_pool.acquire() as conn:
            referrer_id = await conn.fetchval(
                "SELECT telegram_id FROM users WHERE LOWER(referral_code) = LOWER($1)",
                referral_code
            )
        if referrer_id:
            logger.info(f"Found referrer {referrer_id} for {referral_code}")
        else:
            logger.warning(f"No referrer found for {referral_code}")
            await message.reply("Invalid referral code. Proceeding without a referrer.")

    bot_id = app_context.bot.id
    if telegram_id == bot_id:
        logger.error(f"Attempted registration with bot ID {telegram_id}")
        await message.reply("Bot cannot register itself!")
        await state.clear()
        return

    # Generate JWT token
    jwt_secret = os.getenv('JWT_SECRET')  # Set in .env
    token = jwt.encode({
        'telegram_id': telegram_id,
        'referrer_id': referrer_id,
        'exp': time.time() + 600  # 10min expiry
    }, jwt_secret, algorithm='HS256')

    # In TEST_MODE, bypass mini-app and register local test wallet
    if app_context.is_test_mode:
        try:
            from stellar_sdk import Keypair
            test_secret = os.getenv('TEST_SIGNER_SECRET')
            if not test_secret:
                await message.reply("TEST_MODE detected but TEST_SIGNER_SECRET is missing.")
            else:
                test_public = Keypair.from_secret(test_secret).public_key
                async with app_context.db_pool.acquire() as conn:
                    exists = await conn.fetchval("SELECT 1 FROM users WHERE telegram_id = $1", telegram_id)
                    if not exists:
                        await conn.execute(
                            "INSERT INTO users (telegram_id, public_key, referral_code) VALUES ($1, $2, $3)",
                            telegram_id, test_public, f"ref-api-{telegram_id}"
                        )
                    else:
                        await conn.execute(
                            "UPDATE users SET public_key = $1 WHERE telegram_id = $2",
                            test_public, telegram_id
                        )
                await message.reply("Test mode: wallet registered locally. You can proceed without the mini-app.")
        except Exception as e:
            await message.reply(f"Test mode registration failed: {str(e)}")
        await state.clear()
        return

    # Send link button (to lumenbro.com in production)
    mini_app_url = f"https://lumenbro.com/mini-app/index.html?action=register&referrer_id={referrer_id or ''}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Link Telegram to Turnkey", web_app=WebAppInfo(url=mini_app_url))]
    ])
    await message.reply("Open Mini App to save Turnkey API keys securely to Telegram Cloud:", reply_markup=keyboard)
    await state.clear()

async def confirm_seed_saved(callback: types.CallbackQuery, app_context, state: FSMContext):
    telegram_id = callback.from_user.id
    logger.info(f"Received callback: {callback.data}")
    try:
        if f"seed_saved_{telegram_id}" in callback.data:
            logger.info(f"Confirmed seed saved for user {telegram_id}")
            await callback.message.delete()

            # Fetch the bot username dynamically
            bot_info = await callback.message.bot.get_me()
            bot_username = bot_info.username
            founder_link = f"https://t.me/{bot_username}?start=pioneer-signup"

            # Escape special characters for MarkdownV2
            def escape_markdown_v2(text: str) -> str:
                reserved_chars = r"_*[]()~`>#+-=|{}.!"
                for char in reserved_chars:
                    text = text.replace(char, f"\\{char}")
                return text

            escaped_link = escape_markdown_v2(founder_link)
            message_text = (
                f"Great\\! The Message with your secret seed has been deleted, and your wallet is ready\\.\n\n"
                f"To complete your registration, please click below to confirm your pioneer status:\n"
                f"[{escaped_link}]({escaped_link})\n\n"
                f"Note: Pioneer slots are limited to 25 users\\. If the limit is reached, you'll be notified after clicking the link\\."
            )
            try:
                await callback.message.answer(message_text, parse_mode="MarkdownV2")
            except Exception as send_error:
                logger.error(f"Failed to send pioneer link message with MarkdownV2: {str(send_error)}")
                # Fallback: Send the message without Markdown parsing
                await callback.message.answer(
                    f"Great! The Message with your secret seed has been deleted, and your wallet is ready.\n"
                    f"To complete your registration, please click below to confirm your pioneer status:\n"
                    f"{founder_link}\n\n"
 f"Note: Pioneer slots are limited to 25 users. If the limit is reached, you'll be notified after clicking the link.",
                    parse_mode=None
                )

            # Schedule the seed reminder
            asyncio.create_task(send_reminder(callback.message.bot, telegram_id, state))
    except Exception as e:
        logger.error(f"Error in confirm_seed_saved: {str(e)}", exc_info=True)
        # Fallback message in case of any other error
        await callback.message.answer(
            f"Your secret seed has been deleted, and your wallet is ready",
            parse_mode="Markdown"
        )
    await callback.answer()

async def send_reminder(bot, telegram_id, state):
    await asyncio.sleep(10)
    try:
        await bot.send_message(telegram_id, "Reminder: Ensure your seed is securely stored offline!")
        logger.info(f"Sent seed reminder to user {telegram_id}")
    except Exception as e:
        logger.error(f"Failed to send reminder to user {telegram_id}: {str(e)}")

async def process_email(message: types.Message, state: FSMContext, app_context):
    email = message.text.strip()
    data = await state.get_data()
    sub_org_id = data['sub_org_id']
    # Send Web App button
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Set Up Passkey", web_app=WebAppInfo(url=f"https://lumenbro.com/turnkey-auth?orgId={sub_org_id}&email={email}"))]
    ])
    await message.reply("Tap to set up secure passkey:", reply_markup=keyboard)
    await state.clear()  # Continue after in separate callback

async def logout_command(message: types.Message, app_context):
    """Clear session data from database to force re-login"""
    telegram_id = message.from_user.id
    logger.info(f"Logout command: from_user.id={telegram_id}")
    
    async with app_context.db_pool.acquire() as conn:
        # Check if user exists
        exists = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", telegram_id)
        if not exists:
            await message.reply("No wallet registered. Use /register to get started.")
            return
        
        # Clear both legacy and KMS session data
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
        session_data = await conn.fetchrow("""
            SELECT 
                turnkey_session_id,
                temp_api_public_key,
                kms_encrypted_session_key,
                session_expiry,
                session_created_at
            FROM users WHERE telegram_id = $1
        """, telegram_id)
        
        if session_data and all(v is None for v in session_data.values()):
            await message.reply(
                "✅ Session cleared successfully!\n\n"
                "Your session keys have been removed from the database. "
                "You'll need to log in again to perform transactions.\n\n"
                "Use /login to establish a new session."
            )
            logger.info(f"Session cleared for user {telegram_id}")
        else:
            await message.reply("❌ Failed to clear session. Please try again.")
            logger.error(f"Failed to clear session for user {telegram_id}")

async def login_command(message: types.Message, app_context):
    telegram_id = message.from_user.id
    async with app_context.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT turnkey_sub_org_id FROM turnkey_wallets WHERE telegram_id = $1 AND is_active = TRUE", 
            telegram_id
        )
        if not row:
            await message.reply("No wallet—register first.")
            return
        sub_org_id = row['turnkey_sub_org_id']
        email = (await conn.fetchval("SELECT user_email FROM users WHERE telegram_id = $1", telegram_id)) or "unknown@lumenbro.com"
    
    # Use mini-app approach like walletmanagement.py
    mini_app_base = "https://lumenbro.com/mini-app/index.html"
    login_url = f"{mini_app_base}?action=login&orgId={sub_org_id}&email={email}"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Establish Session", web_app=WebAppInfo(url=login_url))
    ]])
    await message.reply("Open to login/establish session:", reply_markup=keyboard)

async def unregister_command(message: types.Message, app_context, streaming_service: StreamingService):
    telegram_id = message.from_user.id
    logger.info(f"Unregister command: from_user.id={telegram_id}, chat_id={message.chat.id}, is_group={message.chat.type == 'group'}")
    chat_id = message.chat.id
    async with app_context.db_pool.acquire() as conn:
        existing = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", telegram_id)
        if not existing:
            await message.reply("No wallet registered.")
            return
        warning_message = (
            "Warning: Unregistering will delete your wallet details, session data, and associated records. "
            "Since your wallet is non-custodial (controlled via Turnkey passkey), ensure you've noted your Sub-Org ID and Key ID for recovery if you have funds.\n\n"
            "Are you sure you want to proceed?"
        )
        confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Yes, Unregister", callback_data=f"confirm_unregister_{telegram_id}"),
             InlineKeyboardButton(text="No, Cancel", callback_data=f"cancel_unregister_{telegram_id}")]
        ])
        await message.reply(warning_message, reply_markup=confirm_keyboard)

async def confirm_unregister(callback: types.CallbackQuery, app_context, streaming_service: StreamingService):
    telegram_id = callback.from_user.id
    chat_id = callback.message.chat.id
    logger.info(f"Confirm unregister: telegram_id={telegram_id}, chat_id={chat_id}")
    try:
        if f"confirm_unregister_{telegram_id}" in callback.data:
            logger.info(f"Proceeding with unregister for user {telegram_id}")
            async with app_context.db_pool.acquire() as conn:
                async with conn.transaction():
                    # 1) Clear session fields (optional but kept for partial unregister semantics)
                    await conn.execute(
                        """
                        UPDATE users SET 
                            turnkey_session_id = NULL, 
                            temp_api_public_key = NULL, 
                            temp_api_private_key = NULL, 
                            kms_encrypted_session_key = NULL,
                            kms_key_id = NULL,
                            session_expiry = NULL,
                            session_created_at = NULL,
                            turnkey_user_id = NULL,
                            user_email = NULL
                        WHERE telegram_id = $1
                        """,
                        telegram_id,
                    )

                    # 2) DELETE dependents FIRST (order matters for FK NO ACTION)
                    await conn.execute("DELETE FROM referrals WHERE referee_id = $1 OR referrer_id = $1", telegram_id)
                    result = await conn.fetchval("SELECT referee_id FROM referrals WHERE referee_id = $1", telegram_id)
                    if result:
                        logger.error(f"Deletion failed: User {telegram_id} still exists in referrals table as referee")
                    else:
                        logger.info(f"User {telegram_id} successfully deleted from referrals table as referee")
                    result = await conn.fetchval("SELECT referrer_id FROM referrals WHERE referrer_id = $1", telegram_id)
                    if result:
                        logger.error(f"Deletion failed: User {telegram_id} still exists in referrals table as referrer")
                    else:
                        logger.info(f"User {telegram_id} successfully deleted from referrals table as referrer")

                    await conn.execute("DELETE FROM copy_trading WHERE user_id = $1", telegram_id)
                    result = await conn.fetchval("SELECT user_id FROM copy_trading WHERE user_id = $1", telegram_id)
                    if result:
                        logger.error(f"Deletion failed: User {telegram_id} still exists in copy_trading table")
                    else:
                        logger.info(f"User {telegram_id} successfully deleted from copy_trading table")

                    await conn.execute("DELETE FROM rewards WHERE user_id = $1", telegram_id)
                    result = await conn.fetchval("SELECT user_id FROM rewards WHERE user_id = $1", telegram_id)
                    if result:
                        logger.error(f"Deletion failed: User {telegram_id} still exists in rewards table")
                    else:
                        logger.info(f"User {telegram_id} successfully deleted from rewards table")

                    await conn.execute("DELETE FROM trades WHERE user_id = $1", telegram_id)
                    result = await conn.fetchval("SELECT user_id FROM trades WHERE user_id = $1", telegram_id)
                    if result:
                        logger.error(f"Deletion failed: User {telegram_id} still exists in trades table")
                    else:
                        logger.info(f"User {telegram_id} successfully deleted from trades table")

                    await conn.execute("DELETE FROM founders WHERE telegram_id = $1", telegram_id)
                    result = await conn.fetchval("SELECT telegram_id FROM founders WHERE telegram_id = $1", telegram_id)
                    if result:
                        logger.error(f"Deletion failed: User {telegram_id} still exists in founders table")
                    else:
                        logger.info(f"User {telegram_id} successfully deleted from founders table")

                    # turnkey_wallets: keep explicit delete to be safe
                    await conn.execute("DELETE FROM turnkey_wallets WHERE telegram_id = $1", telegram_id)
                    result = await conn.fetchval("SELECT telegram_id FROM turnkey_wallets WHERE telegram_id = $1", telegram_id)
                    if result:
                        logger.error(f"Deletion failed: User {telegram_id} still exists in turnkey_wallets table")
                    else:
                        logger.info(f"User {telegram_id} successfully deleted from turnkey_wallets table")

                    # 3) DELETE user only if not legacy (preserve migration data)
                    user_check = await conn.fetchrow(
                        """
                        SELECT source_old_db FROM users WHERE telegram_id = $1
                        """,
                        telegram_id,
                    )
                    if not user_check or not user_check['source_old_db']:
                        await conn.execute("DELETE FROM users WHERE telegram_id = $1", telegram_id)
                        result = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", telegram_id)
                        if result:
                            logger.error(f"Deletion failed: User {telegram_id} still exists in database")
                        else:
                            logger.info(f"User {telegram_id} successfully deleted from database")

            await streaming_service.stop_streaming(chat_id)

            # Call backend to clear any server-side state (e.g., deactivate in Turnkey if applicable)
            import requests
            try:
                response = requests.post('https://lumenbro.com/mini-app/clear', json={'telegram_id': telegram_id})
                if not response.ok:
                    logger.error(f"Backend clear failed: {response.text}")
                else:
                    logger.info(f"Backend clear succeeded for {telegram_id}")
            except Exception as e:
                logger.error(f"Error calling backend clear: {str(e)}")

            # Launch Mini App to clear Telegram Cloud Storage
            mini_app_url = f"https://lumenbro.com/mini-app/index.html?action=unregister"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Complete Unregister in Mini App", web_app=WebAppInfo(url=mini_app_url))]
            ])
            await callback.message.edit_text("DB cleared. Open Mini App to clear cloud storage keys.", reply_markup=keyboard)

        elif f"cancel_unregister_{telegram_id}" in callback.data:
            await callback.message.edit_text("Unregistration cancelled. Your wallet remains active.")
    except Exception as e:
        logger.error(f"Error in confirm_unregister: {str(e)}", exc_info=True)
        await callback.message.edit_text(f"Error during unregistration: {str(e)}")
    await callback.answer()

async def process_buy_sell(callback: types.CallbackQuery, state: FSMContext, app_context):
    """Enhanced buy/sell handler - enhanced buy flow, enhanced sell flow"""
    logger.info(f"Processing buy/sell callback: {callback.data}")
    logger.info(f"Callback from user_id: {callback.from_user.id}")
    action = callback.data
    
    if action == 'buy':
        await state.update_data(action=action)
        await callback.message.reply(
            "Please enter the asset code and issuer for buying in the format: `code:issuer`\n\n"
            "**Example:** `USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN`",
            parse_mode="Markdown"
        )
        await state.set_state(BuySellStates.waiting_for_asset)
    elif action == 'sell':
        # Enhanced sell flow with asset selection
        await state.update_data(action=action)
        logger.info(f"About to call show_sell_asset_selection for user_id: {callback.from_user.id}")
        await show_sell_asset_selection(callback.message, app_context, state, callback.from_user.id)
    
    await callback.answer()

async def process_asset(message: types.Message, state: FSMContext, app_context):
    """Enhanced asset processing with buy menu for buy actions only"""
    asset_input = message.text.strip()
    try:
        code, issuer = asset_input.split(':')
        if not issuer.startswith('G') or len(issuer) != 56:
            raise ValueError("Issuer must be a valid Stellar public key")
        
        await state.update_data(asset_code=code, asset_issuer=issuer)
        
        # Get current action
        data = await state.get_data()
        action = data.get('action', 'buy')
        
        if action == 'buy':
            # Enhanced buy flow with menu
            await show_buy_menu(message, code, issuer, app_context, state)
        else:
            # Keep existing simple flow for sell
            await message.reply("Enter the amount to sell:")
            await state.set_state(BuySellStates.waiting_for_amount)
        
    except ValueError as e:
        logger.error(f"Invalid asset format: {str(e)}", exc_info=True)
        await message.reply(f"Invalid format: {str(e)}. Use: `code:issuer`", parse_mode="Markdown")

async def process_amount(message: types.Message, state: FSMContext, app_context):
    try:
        # Check user access mode (normal vs recovery)
        from utils.user_access import check_user_access, get_access_status_indicator
        
        user_id = message.from_user.id
        org_id, access_mode, access_status = await check_user_access(user_id, app_context.db_pool, app_context)
        
        if not org_id:
            await message.reply("❌ No wallet access. Use `/register` or `/recover <org_id>`.")
            await state.clear()
            return
        
        data = await state.get_data()
        action = data['action']
        asset_code = data['asset_code']
        asset_issuer = data['asset_issuer']
        amount = float(message.text)
        if amount <= 0:
            raise ValueError("Amount must be positive")

        # Add recovery mode note for trading
        recovery_note = ""
        if access_mode == "recovery":
            recovery_note = f"\n\n🔓 **Recovery Mode Trading** {get_access_status_indicator(access_mode, access_status)}\n⚠️ You're in recovery mode (1-hour session).\nFor permanent access, create new Telegram keys at:\nhttps://lumenbro.com/recovery"

        if action == 'buy':
            response, actual_xlm_spent, actual_amount_received, actual_fee_paid, fee_percentage = await perform_buy(
                message.from_user.id, app_context.db_pool, asset_code, asset_issuer, amount, app_context
            )
            await message.reply(
                f"Buy successful. Bought {actual_amount_received:.7f} {asset_code} for {actual_xlm_spent:.7f} XLM\n"
                f"Fee: {actual_fee_paid:.7f} XLM ({fee_percentage:.2f}%)\n"
                f"Tx Hash: {response['hash']}{recovery_note}",
                parse_mode="Markdown"
            )
        elif action == 'sell':
            response, actual_xlm_received, actual_amount_sent, actual_fee_paid, fee_percentage = await perform_sell(
                message.from_user.id, app_context.db_pool, asset_code, asset_issuer, amount, app_context
            )
            await message.reply(
                f"Sell successful. Sold {actual_amount_sent:.7f} {asset_code} for {actual_xlm_received:.7f} XLM\n"
                f"Fee: {actual_fee_paid:.7f} XLM ({fee_percentage:.2f}%)\n"
                f"Tx Hash: {response['hash']}{recovery_note}",
                parse_mode="Markdown"
            )
        else:
            raise ValueError("Invalid action")
    except Exception as e:
        logger.error(f"Error in {action}: {str(e)}", exc_info=True)
        
        # Handle session-related errors gracefully
        error_str = str(e)
        if "No active session" in error_str or "Please login first" in error_str:
            await message.reply(
                "🔴 **Login Required**\n\n"
                "You need to login before trading.\n\n"
                "**How to Login:**\n"
                "• Use `/login` command\n"
                "• Or use Wallet Management → Login\n\n"
                "This will establish a secure session for trading.",
                parse_mode="Markdown"
            )
        elif "Session expired" in error_str:
            await message.reply(
                "⏰ **Session Expired**\n\n"
                "Your trading session has expired.\n\n"
                "**How to Renew:**\n"
                "• Use `/login` command\n"
                "• Or use Wallet Management → Login\n\n"
                "This will create a new secure session.",
                parse_mode="Markdown"
            )
        else:
            # Generic error for other issues
            error_msg = error_str if error_str else "An unexpected error occurred during the transaction."
            await message.reply(f"Error: {error_msg}")
    finally:
        await state.clear()
        dynamic_welcome = await get_welcome_text(message.from_user.id, app_context) 
        await message.reply(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def process_balance(message_or_callback: types.Message | types.CallbackQuery, app_context):
    # Handle both Message (from command) and CallbackQuery (from button)
    if isinstance(message_or_callback, types.Message):
        target = message_or_callback
        user_id = target.from_user.id
        is_callback = False
        logger.debug(f"Processing balance command for user {user_id} via message")
    else:
        target = message_or_callback.message
        user_id = message_or_callback.from_user.id
        is_callback = True
        logger.debug(f"Processing balance callback for user {user_id} via button")

    try:
        # Check user access mode (normal vs recovery)
        from utils.user_access import check_user_access, get_access_status_indicator
        
        org_id, access_mode, access_status = await check_user_access(user_id, app_context.db_pool)
        
        if not org_id:
            await target.reply("""❌ **No Wallet Access**

**New users:** Use `/register` to create a wallet
**Existing users:** Use `/recover <org_id>` for emergency access

**Lost access?** 
1. Visit: https://lumenbro.com/recovery
2. Enter your email → Get OTP → Get organization ID
3. Return here: `/recover <your_org_id>`

Need help? Contact @lumenbrobot support""", parse_mode="Markdown")
            return
        
        status_indicator = get_access_status_indicator(access_mode, access_status)
        
        logger.debug(f"Fetching public key for user {user_id}")
        public_key = await app_context.load_public_key(user_id)
        logger.debug(f"Public key retrieved: {public_key}")

        try:
            logger.debug(f"Loading account for public key {public_key}")
            account = await load_account_async(public_key, app_context)
            logger.debug(f"Account loaded successfully: {account['id']}")

            # Fetch balances, excluding XLM and non-standard assets like liquidity pool shares
            logger.debug("Extracting balances excluding XLM and non-standard assets")
            balance_lines = [
                {"code": b['asset_code'], "issuer": b['asset_issuer'] if b.get('asset_issuer') else 'Unknown', "balance": b['balance']}
                for b in account["balances"]
                if b['asset_type'] in ('credit_alphanum4', 'credit_alphanum12')
            ]
            xlm_balance = float(next((b["balance"] for b in account["balances"] if b["asset_type"] == "native"), "0"))
            logger.debug(f"XLM balance: {xlm_balance}, Number of other assets: {len(balance_lines)}")

            # Calculate XLM usage
            logger.debug("Calculating XLM usage")
            xlm_liabilities = float(next((b["selling_liabilities"] for b in account["balances"] if b["asset_type"] == "native"), "0"))
            subentry_count = account["subentry_count"]
            num_sponsoring = account.get("num_sponsoring", 0)
            num_sponsored = account.get("num_sponsored", 0)
            trustlines = [b for b in account["balances"] if b["asset_type"] in ('credit_alphanum4', 'credit_alphanum12')]
            num_trustlines = len(trustlines)
            base_reserve = 2.0
            subentry_reserve = (subentry_count + num_sponsoring - num_sponsored) * 0.5
            minimum_reserve = base_reserve + subentry_reserve
            available_xlm = max(xlm_balance - xlm_liabilities - minimum_reserve, 0)
            logger.debug(f"Available XLM: {available_xlm}, Minimum reserve: {minimum_reserve}")

            # Identify zero-balance trustlines, cap at 5 for display
            logger.debug("Checking for zero-balance trustlines")
            zero_balance_trustlines = [
                f"{b['asset_code']}:{b['asset_issuer'] if b.get('asset_issuer') else 'Unknown'}"
                for b in trustlines
                if float(b["balance"]) == 0
            ]
            if zero_balance_trustlines:
                display_trustlines = zero_balance_trustlines[:5]
                remaining = len(zero_balance_trustlines) - len(display_trustlines)
                zero_balance_note = (
                    f"\n\n*Note*: You have {len(zero_balance_trustlines)} trustlines with 0 balance, reserving {len(zero_balance_trustlines) * 0.5:.1f} XLM. "
                    f"Remove them to free up XLM:\n- " + "\n- ".join(display_trustlines)
                )
                if remaining > 0:
                    zero_balance_note += f"\n(and {remaining} more)"
                zero_balance_note += f"\nUse /removetrust to remove unused trustlines."
            else:
                zero_balance_note = ""
            logger.debug(f"Zero-balance trustlines: {len(zero_balance_trustlines)}")

            # Build XLM breakdown
            logger.debug("Building XLM breakdown text")
            xlm_breakdown = (
                f"XLM Breakdown:\n"
                f"- Total: {xlm_balance:.7f} XLM\n"
                f"- Available: {available_xlm:.7f} XLM\n"
                f"- Reserved: {minimum_reserve:.7f} XLM\n"
                f"  - Base: {base_reserve:.1f} XLM\n"
                f"  - Trustlines ({num_trustlines}): {num_trustlines * 0.5:.1f} XLM\n"
                f"  - Other Subentries: {(subentry_count - num_trustlines + num_sponsoring - num_sponsored) * 0.5:.1f} XLM"
            )
            if xlm_liabilities > 0:
                xlm_breakdown += f"\n- Liabilities (Offers): {xlm_liabilities:.7f} XLM"
            logger.debug("XLM breakdown constructed")

            # Fetch XLM/USD price
            logger.debug("Fetching XLM/USD price")
            total_value_xlm = xlm_balance  # Start with XLM balance
            total_value_usd = 0.0
            xlm_usd_price = await app_context.price_service.fetch_xlm_usd_price()
            logger.debug(f"XLM/USD price fetched: {xlm_usd_price}")

            # Add XLM balance USD value
            if xlm_usd_price:
                xlm_usd_value = xlm_balance * xlm_usd_price
                total_value_usd += xlm_usd_value
                logger.debug(f"XLM USD value: {xlm_balance} XLM * {xlm_usd_price} USD/XLM = {xlm_usd_value} USD")
            else:
                logger.warning("XLM/USD price unavailable, excluding XLM from USD total")

            # Track assets with zero price and the latest price timestamp
            zero_price_assets = []
            latest_price_timestamp = None

            logger.debug("Fetching asset values for other assets")
            for i, asset in enumerate(balance_lines):
                logger.debug(f"Processing asset {i+1}/{len(balance_lines)}: {asset['code']}:{asset['issuer']}")
                value_in_xlm, value_in_usd = await app_context.price_service.get_asset_value(
                    asset['code'], asset['issuer'], asset['balance']
                )
                logger.debug(f"Asset {asset['code']}: Value in XLM = {value_in_xlm}, Value in USD = {value_in_usd}")
                asset['value_in_xlm'] = value_in_xlm
                asset['value_in_usd'] = value_in_usd
                if value_in_xlm == 0.0:
                    zero_price_assets.append(asset['code'])
                # Update the latest price timestamp from the cache
                cache_key = f"{asset['code']}:{asset['issuer']}"
                if cache_key in app_context.price_service.price_cache:
                    timestamp = app_context.price_service.price_cache[cache_key][1]
                    if latest_price_timestamp is None or timestamp > latest_price_timestamp:
                        latest_price_timestamp = timestamp
                total_value_xlm += value_in_xlm
                total_value_usd += value_in_usd
            logger.debug(f"Total wallet value: {total_value_xlm} XLM, ${total_value_usd} USD")

            # Build content_text with asset values
            logger.debug("Building balance text with asset values")
            balance_text_lines = []
            for asset in balance_lines:
                value_display = f"≈ {asset['value_in_xlm']:.4f} XLM"
                if xlm_usd_price and asset['value_in_usd'] > 0:
                    value_display += f" (${asset['value_in_usd']:.2f})"
                asset_line = (
                    f"`{asset['code']}:{asset['issuer']}`: {asset['balance']} {value_display}"
                )
                balance_text_lines.append(asset_line)
            balance_text = "\n\n".join(balance_text_lines)  # Add extra newline between rows
            logger.debug("Balance text constructed")

            # Add total value
            logger.debug("Adding total wallet value to output")
            total_value_text = f"\n\n**Total Wallet Value**\n≈ {total_value_xlm:.4f} XLM"
            if xlm_usd_price and total_value_usd > 0:
                total_value_text += f" (${total_value_usd:.2f})"

            # Add timestamp and zero-price warning
            additional_notes = ""
            if latest_price_timestamp:
                additional_notes += f"\n\n*Prices updated at {latest_price_timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC*"
            if zero_price_assets:
                additional_notes += f"\n\n*Warning*: Price unavailable for {', '.join(zero_price_assets)}. Values may be inaccurate."

            # Construct message without header
            logger.debug("Constructing final message")
            max_message_length = 4096
            header = f"💰 **Wallet Balance** {status_indicator}\n\nYour wallet: `{public_key}`\nYour balances:\n"
            footer = ""
            if available_xlm < 0.1:
                footer += f"\n\nYour available XLM is low ({available_xlm:.7f} XLM). Please fund your account to perform transactions."
            footer += zero_balance_note
            footer += total_value_text
            footer += additional_notes
            
            # Add recovery mode warning if applicable
            if access_mode == "recovery":
                footer += "\n\n⚠️ *Recovery mode active. Session expires in 1 hour.*"

            # Build content_text without the header
            content_text = f"{xlm_breakdown}\n\nOther Assets:\n{balance_text}" if balance_text else f"{xlm_breakdown}"

            # Use pagination logic
            logger.debug("Applying pagination logic")
            available_length = max_message_length - len(header) - len(footer)
            messages = []
            current_message = header
            lines = content_text.split("\n")

            for line in lines:
                if len(current_message) + len(line) + 1 > max_message_length - len(footer):
                    current_message += footer
                    messages.append(current_message)
                    current_message = header
                current_message += line + "\n"

            if current_message != header:
                current_message += footer
                messages.append(current_message)

            logger.debug(f"Sending {len(messages)} message(s) to user")
            for i, msg in enumerate(messages):
                if len(messages) > 1:
                    msg = f"Page {i+1}/{len(messages)}\n{msg}"
                logger.debug(f"Sending message {i+1}/{len(messages)}")
                await target.reply(msg, parse_mode="Markdown")
                logger.debug(f"Message {i+1}/{len(messages)} sent")

        except NotFoundError:
            logger.debug("Account not found, sending unfunded message")
            recovery_warning = "\n\n⚠️ *Recovery mode active. Session expires in 1 hour.*" if access_mode == "recovery" else ""
            await target.reply(
                f"💰 **Wallet Balance** {status_indicator}\n\n"
                f"Your wallet: `{public_key}`\n"
                f"Your account isn't funded yet. To activate it, send XLM to your public key from an exchange or wallet (e.g., Coinbase, Kraken, Lobstr).{recovery_warning}",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error fetching balance: {str(e)}", exc_info=True)
        await target.reply(f"Error fetching balance: {str(e)}")
    if is_callback:
        logger.debug("Acknowledging callback query")
        await message_or_callback.answer()

async def process_register_callback(callback: types.CallbackQuery, app_context, state: FSMContext):
    await register_command(callback.message, app_context, state)
    await callback.answer()

async def process_copy_trading_callback(callback: types.CallbackQuery, app_context, streaming_service: StreamingService):
    user_id = callback.from_user.id
    await copy_trade_menu_command(callback.message, streaming_service, user_id=user_id, app_context=app_context)
    await callback.answer()

async def process_withdraw(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.reply("Please specify the asset you want to withdraw (e.g., XLM or USDC:issuer_address).")
    await state.set_state(WithdrawStates.waiting_for_asset)
    await callback.answer()

async def process_withdraw_asset(message: types.Message, state: FSMContext):
    asset_input = message.text.strip()
    if asset_input.lower() == "xlm":
        asset = Asset.native()
    else:
        try:
            code, issuer = asset_input.split(':')
            Keypair.from_public_key(issuer)
            asset = Asset(code, issuer)
        except:
            await message.reply("Invalid asset format. Use 'XLM' or 'code:issuer'")
            return
    await state.update_data(asset=asset)
    await message.reply("Please enter the destination address.")
    await state.set_state(WithdrawStates.waiting_for_address)

async def process_withdraw_address(message: types.Message, state: FSMContext):
    address = message.text.strip()
    try:
        Keypair.from_public_key(address)
    except:
        await message.reply("Invalid Stellar public key.")
        return
    await state.update_data(address=address)
    await message.reply("Please enter the amount to withdraw.")
    await state.set_state(WithdrawStates.waiting_for_amount)

async def process_withdraw_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0:
            raise ValueError("Amount must be positive")
    except ValueError as e:
        await message.reply(f"Invalid amount: {str(e)}")
        return
    data = await state.get_data()
    asset = data['asset']
    address = data['address']
    await state.update_data(amount=amount)
    asset_str = "XLM" if asset.is_native() else f"{asset.code}:{asset.issuer}"
    confirmation_text = f"Please confirm the withdrawal:\nAsset: {asset_str}\nAmount: {amount}\nDestination: {address}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm", callback_data="confirm_withdraw"),
         InlineKeyboardButton(text="Cancel", callback_data="cancel_withdraw")]
    ])
    await message.reply(confirmation_text, reply_markup=keyboard)
    await state.set_state(WithdrawStates.waiting_for_confirmation)

async def process_withdraw_confirmation(callback: types.CallbackQuery, state: FSMContext, app_context):
    if callback.data == "confirm_withdraw":
        # Check user access mode (normal vs recovery)
        from utils.user_access import check_user_access, get_access_status_indicator
        
        user_id = callback.from_user.id
        org_id, access_mode, access_status = await check_user_access(user_id, app_context.db_pool, app_context)
        
        if not org_id:
            await callback.message.reply("❌ No wallet access. Use `/register` or `/recover <org_id>`.")
            await state.clear()
            await callback.answer()
            return
        
        data = await state.get_data()
        asset = data['asset']
        amount = data['amount']
        destination = data['address']
        
        try:
            # Add recovery mode warning for payments
            if access_mode == "recovery":
                recovery_note = "\n\n🔓 **Recovery Mode Payment**\n⚠️ You're in recovery mode (1-hour session).\nFor permanent access, create new Telegram keys at:\nhttps://lumenbro.com/recovery"
            else:
                recovery_note = ""
            
            from services.trade_services import perform_withdraw
            response = await perform_withdraw(callback.from_user.id, app_context.db_pool, asset, amount, destination, app_context)
            await callback.message.reply(f"Withdrawal successful. Tx Hash: {response['hash']}{recovery_note}", parse_mode="Markdown")
        except Exception as e:
            await callback.message.reply(f"Withdrawal failed: {str(e)}")
    else:
        await callback.message.reply("Withdrawal cancelled.")
    await state.clear()
    await callback.answer()

async def export_rewards_command(message: types.Message, app_context):
    telegram_id = message.from_user.id
    admin_id = os.getenv("ADMIN_TELEGRAM_ID")
    if str(telegram_id) != admin_id:
        await message.reply("You are not authorized to use this command.")
        return

    output_file = f"referral_rewards_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    try:
        exported_file_path, total_payout, payout_list = await export_unpaid_rewards(app_context.db_pool, app_context.db_pool, output_file)
        if exported_file_path:
            await message.reply(f"Referral rewards exported to {exported_file_path}")
        else:
            await message.reply("No unpaid rewards to export.")
    except Exception as e:
        logger.error(f"Error exporting unpaid rewards: {str(e)}", exc_info=True)
        await message.reply("An error occurred while exporting unpaid rewards. Please try again later.")

async def manual_payout_command(message: types.Message, app_context):
    telegram_id = message.from_user.id
    admin_id = os.getenv("ADMIN_TELEGRAM_ID")
    if str(telegram_id) != admin_id:
        await message.reply("You are not authorized to use this command.")
        return
    chat_id = message.chat.id
    await daily_payout(app_context.db_pool, app_context.bot, chat_id, app_context)

async def rankings_command(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="View Wallet Rankings", web_app=WebAppInfo(url="https://lumenbro.com/"))]
    ])
    await message.reply("Click below to view wallet rankings:", reply_markup=keyboard)

async def help_faq_command(message: types.Message):
    faq_text = (
        "*Photon Bot Help & FAQ*\n\n"
        "*What is @lumenbrobot?*\n"
        "Your gateway to trading on the Stellar network! Buy, sell, manage assets, follow top traders with copy trading, "
        "and earn rewards by inviting friends.\n\n"
        "*How do I start?*\n"
        "Use /start to check your wallet or begin registration. You'll get a dedicated wallet for bot trading.\n\n"
        "*🚀 NEW: Enhanced Trading Commands*\n"
        "• **`/buy`** - Enhanced buy with XLM amounts & market data\n"
        "• **`/sell`** - Enhanced sell with asset selection & percentages\n"
        "• **`/balance`** - Check wallet balance & asset values\n\n"
        "*How much are fees?*\n"
        "1% of all transactions for direct registration 10% discount if referred, wallet ranking report service is free. (dedicated Horizon and RPC servers are being used in both bot and walletrank)\n\n"
        "*What can I do?*\n"
        "- *Buy/Sell*: Trade assets with enhanced menus (use `/buy` or `/sell` commands).\n"
        "- *Enhanced Buy*: XLM amounts, market data, custom amounts with persistence.\n"
        "- *Enhanced Sell*: Asset selection, percentage-based selling, market info.\n"
        "- *Check Balance*: View your XLM and asset balances, includes reserve calculation and net available XLM.\n"
        "- *Copy Trading*: Streams transactions from any G-address wallet with Horizon AIOHTTP and copies the trade. Multiplier, fixed-amount and slippage settings supported per copied wallet.\n"
        "- *Withdraw*: Send XLM or assets to another Stellar address.\n"
        "- *Referrals*: Invite friends with your referral code to earn rewards.\n"
        "- *Trustlines*: Add (/addtrust) or remove (/removetrust) assets to trade.\n"
        "- *Help*: Use /help for this guide.\n\n"
        "*How do I fund my wallet?*\n"
        "Send XLM to your wallet's public key from an exchange (e.g., Coinbase, Kraken, Lobstr). "
        "Fund only what you plan to trade to keep your main wallets safe.\n\n"
        "*Do i manually have to add trustlines for copy-trading or buy/sell?*\n"
        "No, the bot will automatically add trustlines for you when you perform a buy/sell or copy-trade.\n\n"
        "*How do I recover my wallet?*\n"
        "During registration, you receive a 24-word mnemonic. Store it offline (e.g., paper, USB). "
        "To recover, import it into a Stellar wallet like Xbull or Lobstr.\n\n"
        "*Is my wallet secure?*\n"
        "Your wallet is generated in a secure, isolated environment with industry-standard encryption. "
        "Your funds are safe as long as you keep your mnemonic private and delete the registration message after saving it.\n\n"
        "*Tips*:\n"
        "- Never share your mnemonic.\n"
        "- Use /removetrust to free up XLM from unused trustlines.\n"
        "- Use `/buy` and `/sell` for enhanced trading experience.\n"
        "- Check /help anytime for guidance.\n\n"
        "- For better wallet managment import mnemonic into Xbull or Lobstr and use the bot as a trading tool.\n\n"
        "*What Soroban functions are supported?*:\n"
        "So far can copy trades from AQUA and Soroswap Routers, has a fallback to SDEX if Soroban copytrade fails. "
        "More functions will be added in the future, for now only issued assets with SAC contracts and copy trading only, no direct buy/sell.\n\n"
        "*Need more help?*\n"
        "Message @lumenbrobot support in Telegram."
    )
    await message.reply(faq_text, parse_mode="Markdown")

async def help_faq_callback(callback: types.CallbackQuery):
    faq_text = (
        "*Photon Bot Help & FAQ*\n\n"
        "*What is @lumenbrobot?*\n"
        "Your gateway to trading on the Stellar network! Buy, sell, manage assets, follow top traders with copy trading, "
        "and earn rewards by inviting friends.\n\n"
        "*How do I start?*\n"
        "Use /start to check your wallet or begin registration. You'll get a dedicated wallet for bot trading.\n\n"
        "*How much are fees?*\n"
        "1% of all transactions for direct registration 10% discount if referred, wallet ranking report service is free. (dedicated Horizon and RPC servers are being used for both bot and walletrank)\n\n"
        "*What can I do?*\n"
        "- *Buy/Sell*: Trade assets like USDC, SHX, ETH (use buttons after /start).\n"
        "- *Check Balance*: View your XLM and asset balances, includes reserve calculation and net available XLM.\n"
        "- *Copy Trading*: Streams transactions from any G-address wallet with Horizon AIOHTTP and copies the trade. Multiplier, fixed-amount and slippage settings supported per copied wallet.\n"
        "- *Withdraw*: Send XLM or assets to another Stellar address.\n"
        "- *Referrals*: Invite friends with your referral code to earn rewards.\n"
        "- *Trustlines*: Add (/addtrust) or remove (/removetrust) assets to trade.\n"
        "- *Help*: Use /help for/living this guide.\n\n"
        "*How do I fund my wallet?*\n"
        "Send XLM to your wallet's public key from an exchange (e.g., Coinbase, Kraken, Lobstr). "
        "Fund only what you plan to trade to keep your main wallets safe.\n\n"
        "*Do i manually have to add trustlines for copy-trading or buy/sell?*\n"
        "No, the bot will automatically add trustlines for you when you perform a buy/sell or copy-trade.\n\n"
        "*How do I recover my wallet?*\n"
        "During registration, you receive a 24-word mnemonic. Store it offline (e.g., paper, USB). "
        "To recover, import it into a Stellar wallet like Xbull or Lobstr.\n\n"
        "*Is my wallet secure?*\n"
        "Your wallet is generated in a secure, isolated environment with industry-standard encryption. "
        "Your funds are safe as long as you keep your mnemonic private and delete the registration message after saving it.\n\n"
        "*Tips*:\n"
        "- Never share your mnemonic.\n"
        "- Use /removetrust to free up XLM from unused trustlines.\n"
        "- Check /help anytime for guidance.\n\n"
        "- For better wallet managment import mnemonic into Xbull or Lobstr and use the bot as a trading tool.\n\n"
        "*What Soroban functions are supported?*:\n"
        "So far can copy trades from AQUA and Soroswap Routers, has a fallback to SDEX if Soroban copytrade fails. "
        "More functions will be added in the future, for now only issued assets with SAC contracts and copy trading only, no direct buy/sell.\n\n"
        "*Need more help?*\n"
        "Message @lumenbrobot support in Telegram."
    )
    await callback.message.reply(faq_text, parse_mode="Markdown")
    await callback.answer()

async def process_add_trustline(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.reply("Please enter the asset to add trustline for in the format: code:issuer")
    await state.set_state(TrustlineStates.waiting_for_asset_to_add)
    await callback.answer()

async def process_remove_trustline(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.reply("Please enter the asset to remove trustline for in the format: code:issuer")
    await state.set_state(TrustlineStates.waiting_for_asset_to_remove)
    await callback.answer()

async def add_trust_command(message: types.Message, state: FSMContext):
    await message.reply("Please enter the asset to add trustline for in the format: code:issuer")
    await state.set_state(TrustlineStates.waiting_for_asset_to_add)

async def remove_trust_command(message: types.Message, state: FSMContext):
    await message.reply("Please enter the asset to remove trustline for in the format: code:issuer")
    await state.set_state(TrustlineStates.waiting_for_asset_to_remove)


async def process_add_trustline_asset(message: types.Message, state: FSMContext, app_context):
    asset_input = message.text.strip()
    try:
        code, issuer = asset_input.split(':')
        if not issuer.startswith('G') or len(issuer) != 56:
            raise ValueError("Issuer must be a valid Stellar public key")

        from services.trade_services import perform_add_trustline
        response = await perform_add_trustline(message.from_user.id, app_context.db_pool, code, issuer, app_context)
        await message.reply(f"Trustline added successfully for {code}:{issuer}. Tx Hash: {response['hash']}")
    except Exception as e:
        logger.error(f"Error adding trustline: {str(e)}", exc_info=True)
        await message.reply(f"Error adding trustline: {str(e)}")
    finally:
        await state.clear()
        dynamic_welcome = await get_welcome_text(message.from_user.id, app_context) 
        await message.reply(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def process_remove_trustline_asset(message: types.Message, state: FSMContext, app_context):
    asset_input = message.text.strip()
    try:
        code, issuer = asset_input.split(':')
        if not issuer.startswith('G') or len(issuer) != 56:
            raise ValueError("Issuer must be a valid Stellar public key")

        from services.trade_services import perform_remove_trustline
        response = await perform_remove_trustline(message.from_user.id, app_context.db_pool, code, issuer, app_context)
        await message.reply(f"Trustline removed successfully for {code}:{issuer}. Tx Hash: {response['hash']}")
    except Exception as e:
        logger.error(f"Error removing trustline: {str(e)}", exc_info=True)
        await message.reply(f"Error removing trustline: {str(e)}")
    finally:
        await state.clear()
        dynamic_welcome = await get_welcome_text(message.from_user.id, app_context) 
        await message.reply(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def process_wallet_management(app_context, callback: types.CallbackQuery):
    await process_wallet_management_callback(callback, app_context)       

def register_main_handlers(dp, app_context, streaming_service):
    async def start_handler(message: types.Message, state: FSMContext):
        await start_command(message, app_context, streaming_service, state)
    dp.message.register(start_handler, Command("start"))

    # Buy and Sell command handlers
    async def buy_command_handler(message: types.Message, state: FSMContext):
        await buy_command(message, state, app_context)
    dp.message.register(buy_command_handler, Command("buy"))
    
    async def sell_command_handler(message: types.Message, state: FSMContext):
        await sell_command(message, state, app_context)
    dp.message.register(sell_command_handler, Command("sell"))

    dp.message.register(cancel_command, Command("cancel"))

    async def register_handler(message: types.Message, state: FSMContext):
        await register_command(message, app_context, state)
    dp.message.register(register_handler, Command("register"))

    async def buy_sell_handler(callback: types.CallbackQuery, state: FSMContext):
        await process_buy_sell(callback, state, app_context)
    dp.callback_query.register(buy_sell_handler, lambda c: c.data in ["buy", "sell"])
    
    # Enhanced buy menu handlers
    async def buy_menu_callback_handler(callback: types.CallbackQuery, state: FSMContext):
        await handle_buy_menu_callback(callback, state, app_context)
    dp.callback_query.register(
        buy_menu_callback_handler,
        lambda c: c.data.startswith("qb:") or c.data in ["ca", "rf", "back_to_main", "insufficient", "clear_ca"]
    )
    
    # Asset processing handlers
    async def asset_handler(message: types.Message, state: FSMContext):
        await process_asset(message, state, app_context)
    dp.message.register(asset_handler, BuySellStates.waiting_for_asset)
    
    # Custom amount handler
    async def custom_amount_handler(message: types.Message, state: FSMContext):
        await process_custom_amount(message, state, app_context)
    dp.message.register(custom_amount_handler, BuySellStates.waiting_for_custom_amount)

    async def amount_handler(message: types.Message, state: FSMContext):
        await process_amount(message, state, app_context)
    dp.message.register(amount_handler, BuySellStates.waiting_for_amount)

    async def balance_callback_handler(callback: types.CallbackQuery):
        await process_balance(callback, app_context)
    dp.callback_query.register(balance_callback_handler, lambda c: c.data == "balance")

    async def balance_command_handler(message: types.Message):
        await process_balance(message, app_context)
    dp.message.register(balance_command_handler, Command("balance"))
    dp.message.register(balance_command_handler, Command("checkbalance"))

    async def register_callback_handler(callback: types.CallbackQuery, state: FSMContext):
        await process_register_callback(callback, app_context, state)
    dp.callback_query.register(register_callback_handler, lambda c: c.data == "register")

    async def copy_trading_handler(callback: types.CallbackQuery):
        await process_copy_trading_callback(callback, app_context, streaming_service)
    dp.callback_query.register(copy_trading_handler, lambda c: c.data == "copy_trading")

    # Referrals handler
    async def referrals_handler(callback: types.CallbackQuery, state: FSMContext):
        from handlers.referrals import referrals_menu
        await referrals_menu(callback, app_context, state)
    dp.callback_query.register(referrals_handler, lambda c: c.data == "wallets")

    async def unregister_handler(message: types.Message):
        await unregister_command(message, app_context, streaming_service)
    dp.message.register(unregister_handler, Command("unregister"))

    dp.callback_query.register(process_withdraw, lambda c: c.data == "withdraw")
    dp.message.register(process_withdraw_asset, WithdrawStates.waiting_for_asset)
    dp.message.register(process_withdraw_address, WithdrawStates.waiting_for_address)
    dp.message.register(process_withdraw_amount, WithdrawStates.waiting_for_amount)
    async def withdraw_confirmation_handler(callback: types.CallbackQuery, state: FSMContext):
        await process_withdraw_confirmation(callback, state, app_context)
    dp.callback_query.register(withdraw_confirmation_handler, WithdrawStates.waiting_for_confirmation)

    async def seed_saved_wrapper(callback: types.CallbackQuery, state: FSMContext):
        return await confirm_seed_saved(callback, app_context, state)
    dp.callback_query.register(
        seed_saved_wrapper,
        lambda c: c.data.startswith("seed_saved_")
    )

    async def unregister_wrapper(callback: types.CallbackQuery):
        return await confirm_unregister(callback, app_context, streaming_service)
    dp.callback_query.register(
        unregister_wrapper,
        lambda c: c.data.startswith(("confirm_unregister_", "cancel_unregister_"))
    )

    async def export_handler(message: types.Message):
        await export_rewards_command(message, app_context)
    dp.message.register(export_handler, Command("export_rewards"))

    async def referral_code_handler(message: types.Message, state: FSMContext):
        await process_referral_code(message, state, app_context)
    dp.message.register(referral_code_handler, ReferralStates.referral_code)

    async def manual_payout_handler(message: types.Message):
        await manual_payout_command(message, app_context)
    dp.message.register(manual_payout_handler, Command("manual_payout"))

    dp.message.register(help_faq_command, Command("help"))
    dp.callback_query.register(help_faq_callback, lambda c: c.data == "help_faq")

    dp.callback_query.register(process_add_trustline, lambda c: c.data == "add_trustline")
    dp.callback_query.register(process_remove_trustline, lambda c: c.data == "remove_trustline")

    dp.message.register(add_trust_command, Command("addtrust"))
    dp.message.register(remove_trust_command, Command("removetrust"))

    async def add_trustline_asset_handler(message: types.Message, state: FSMContext):
        await process_add_trustline_asset(message, state, app_context)
    dp.message.register(add_trustline_asset_handler, TrustlineStates.waiting_for_asset_to_add)

    async def remove_trustline_asset_handler(message: types.Message, state: FSMContext):
        await process_remove_trustline_asset(message, state, app_context)
    dp.message.register(remove_trustline_asset_handler, TrustlineStates.waiting_for_asset_to_remove)

    dp.message.register(rankings_command, Command("rankings"))

    # Process email handler
    async def process_email_handler(message: types.Message, state: FSMContext):
        await process_email(message, state, app_context)
    dp.message.register(process_email_handler, RegisterStates.waiting_for_email)

    dp.callback_query.register(partial(process_wallet_management, app_context), lambda c: c.data == "wallet_management")
    dp.callback_query.register(process_main_menu_callback, lambda c: c.data == "main_menu")  # No partial needed if no extra args

    # Migration callback handlers
    async def migration_export_handler(callback: types.CallbackQuery):
        await process_migration_export(callback, app_context)
    dp.callback_query.register(migration_export_handler, lambda c: c.data == "export_legacy_wallet")

    async def migration_notified_later_handler(callback: types.CallbackQuery):
        await process_migration_notified_later(callback, app_context)
    dp.callback_query.register(migration_notified_later_handler, lambda c: c.data == "migration_notified_later")

    async def migration_help_handler(callback: types.CallbackQuery):
        await process_migration_help(callback)
    dp.callback_query.register(migration_help_handler, lambda c: c.data == "migration_help")

    async def register_new_wallet_handler(callback: types.CallbackQuery):
        await process_register_new_wallet(callback, app_context)
    dp.callback_query.register(register_new_wallet_handler, lambda c: c.data == "register_new_wallet")

    # New handlers for export message actions
    async def delete_export_message_handler(callback: types.CallbackQuery):
        await delete_export_message(callback)
    dp.callback_query.register(delete_export_message_handler, lambda c: c.data == "delete_export_message")

    async def continue_turnkey_registration_handler(callback: types.CallbackQuery):
        await continue_turnkey_registration(callback, app_context)
    dp.callback_query.register(continue_turnkey_registration_handler, lambda c: c.data == "continue_turnkey_registration")

    async def login_handler(message: types.Message):
        await login_command(message, app_context)
    dp.message.register(login_handler, Command("login"))

    async def logout_handler(message: types.Message):
        await logout_command(message, app_context)
    dp.message.register(logout_handler, Command("logout"))

    # Custom amount handler
    async def custom_amount_handler(message: types.Message, state: FSMContext):
        await process_custom_amount(message, state, app_context)
    dp.message.register(custom_amount_handler, BuySellStates.waiting_for_custom_amount)

    # Enhanced sell menu handlers
    async def sell_menu_callback_handler(callback: types.CallbackQuery, state: FSMContext):
        await handle_sell_menu_callback(callback, state, app_context)
    dp.callback_query.register(
        sell_menu_callback_handler,
        lambda c: c.data.startswith("sell_pct:") or c.data in ["sell_custom_pct", "sell_refresh", "sell_back_to_assets"]
    )
    
    # Sell asset selection handlers
    async def sell_asset_selection_handler(callback: types.CallbackQuery, state: FSMContext):
        await handle_sell_asset_selection(callback, state, app_context)
    dp.callback_query.register(
        sell_asset_selection_handler,
        lambda c: c.data.startswith("sa:")
    )
    
    # Custom sell percentage handler
    async def custom_sell_percentage_handler(message: types.Message, state: FSMContext):
        await process_custom_sell_percentage(message, state, app_context)
    dp.message.register(custom_sell_percentage_handler, BuySellStates.waiting_for_sell_percentage)

async def show_buy_menu(message: types.Message, asset_code: str, asset_issuer: str, app_context, state: FSMContext):
    """Show the enhanced buy menu with asset info and quick buy options"""
    
    # Fetch asset information from Stellar Expert using PriceService
    asset_info = await app_context.price_service.get_asset_info(asset_code, asset_issuer)
    
    # Get user's XLM balance
    public_key = await app_context.load_public_key(message.from_user.id)
    account = await load_account_async(public_key, app_context)
    available_xlm = calculate_available_xlm(account)
    
    logger.info(f"Creating buy menu for {asset_code}:{asset_issuer}, available XLM: {available_xlm}")
    
    # Create menu text
    menu_text = create_buy_menu_text(asset_info, asset_code, asset_issuer, available_xlm)
    
    # Create keyboard with specified XLM amounts
    keyboard = await create_buy_menu_keyboard(asset_code, asset_issuer, available_xlm, message.from_user.id, app_context)
    
    # Delete the asset input message to clean up the chat
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Could not delete asset input message: {e}")
    
    # Use send_message instead of reply since we deleted the original message
    await message.bot.send_message(message.chat.id, menu_text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(BuySellStates.buy_menu_displayed)

def create_buy_menu_text(asset_info, asset_code: str, asset_issuer: str, available_xlm: float) -> str:
    """Create the text for the buy menu using PriceService data"""
    
    text = f"**Buy {asset_code}** — ({asset_info.get('name', asset_code) if asset_info else asset_code})\n"
    text += f"`{asset_issuer}`\n\n"
    
    if asset_info:
        # Market data section using PriceService data
        text += "**📊 Market Data:**\n"
        if asset_info.get('price_usd'):
            text += f"• Price: ${asset_info['price_usd']:.8f}\n"
        if asset_info.get('price_xlm'):
            text += f"• Price: {asset_info['price_xlm']:.8f} XLM\n"
        if asset_info.get('market_cap_usd'):
            text += f"• Market Cap: ${asset_info['market_cap_usd']:,.0f}\n"
        if asset_info.get('volume_24h'):
            text += f"• 24h Volume: ${asset_info['volume_24h']:,.0f}\n"
        if asset_info.get('supply'):
            text += f"• Supply: {asset_info['supply']:,.0f}\n"
        
        if asset_info.get('domain'):
            text += f"• Domain: {asset_info['domain']}\n"
        if asset_info.get('tags'):
            text += f"• Tags: {', '.join(asset_info['tags'])}\n"
    
    # User balance
    text += f"\n**💰 Your Balance:** {available_xlm:.7f} XLM\n\n"
    
    # Quick buy preview using PriceService
    if asset_info and asset_info.get('price_xlm') and asset_info['price_xlm'] > 0:
        tokens_per_xlm = 1 / asset_info['price_xlm']
        text += f"**💡 Quick Buy Preview:**\n"
        # Format based on the magnitude of the number
        if tokens_per_xlm >= 1000:
            # For large numbers, use comma formatting with no decimals
            formatted_tokens = f"{tokens_per_xlm:,.0f}"
        elif tokens_per_xlm >= 1:
            # For medium numbers, show up to 2 decimal places
            formatted_tokens = f"{tokens_per_xlm:.2f}"
        else:
            # For small numbers (like stablecoins), show more decimal places
            formatted_tokens = f"{tokens_per_xlm:.6f}"
        
        text += f"1 XLM ≈ {formatted_tokens} {asset_code}\n\n"
    
    text += "**Select an amount to buy:**"
    
    return text

async def create_buy_menu_keyboard(asset_code: str, asset_issuer: str, available_xlm: float, telegram_id: int, app_context) -> InlineKeyboardMarkup:
    """Create the keyboard for the buy menu with specified XLM amounts"""
    
    # Specified XLM amounts (25, 50, 100, 200)
    quick_amounts = [25, 50, 100, 200]
    
    logger.info(f"Creating keyboard with amounts: {quick_amounts}, available XLM: {available_xlm}")
    
    # Get user's saved custom amount
    custom_amount = await get_custom_amount(telegram_id, app_context.db_pool)
    
    # Create keyboard rows
    keyboard_rows = []
    
    # Quick buy buttons (2 per row for better visibility)
    for i in range(0, len(quick_amounts), 2):
        row = []
        for j in range(2):
            if i + j < len(quick_amounts):
                amount = quick_amounts[i + j]
                # Check if user can afford this amount
                can_afford = amount <= available_xlm
                button_text = f"{amount} XLM"
                if not can_afford:
                    button_text += " (insufficient)"
                
                logger.info(f"Creating button: {button_text}, can_afford: {can_afford}")
                
                # Use shorter callback data to avoid BUTTON_DATA_INVALID error
                # Store asset info in state instead of callback data
                row.append(InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"qb:{amount}" if can_afford else "insufficient"
                ))
        keyboard_rows.append(row)
    
    # Custom amount button with checkmark if saved
    custom_button_text = "💰 Custom Amount"
    if custom_amount:
        custom_button_text = f"✅ {custom_amount} XLM"
    
    # Custom amount and navigation buttons
    keyboard_rows.extend([
        [InlineKeyboardButton(text=custom_button_text, callback_data="ca")]
    ])
    
    # Add clear custom amount button if there's a saved amount
    if custom_amount:
        keyboard_rows.append([InlineKeyboardButton(text="🗑️ Clear Custom Amount", callback_data="clear_ca")])
    
    keyboard_rows.extend([
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="rf")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_main")]
    ])
    
    logger.info(f"Created keyboard with {len(keyboard_rows)} rows, custom amount: {custom_amount}")
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

async def handle_buy_menu_callback(callback: types.CallbackQuery, state: FSMContext, app_context):
    """Handle callbacks from the buy menu"""
    try:
        data = callback.data
        
        if data.startswith("qb:"):
            # Handle quick buy with XLM amounts
            _, xlm_amount = data.split(":")
            # Get asset info from state
            state_data = await state.get_data()
            asset_code = state_data.get('asset_code')
            asset_issuer = state_data.get('asset_issuer')
            
            if asset_code and asset_issuer:
                await handle_quick_buy_xlm(callback, asset_code, asset_issuer, float(xlm_amount), app_context)
            else:
                await callback.message.reply("❌ Asset information not found. Please try again.")
        
        elif data == "insufficient":
            # Handle insufficient balance case
            await callback.message.reply("❌ Insufficient XLM balance for this amount. Please select a smaller amount or add more XLM to your wallet.")
            
        elif data == "ca":
            # Handle custom amount input
            logger.info(f"Custom amount button clicked for user {callback.from_user.id}")
            
            # Get asset info from state
            state_data = await state.get_data()
            asset_code = state_data.get('asset_code')
            asset_issuer = state_data.get('asset_issuer')
            
            logger.info(f"Asset info from state: {asset_code}:{asset_issuer}")
            
            if asset_code and asset_issuer:
                # Check if user has a saved custom amount
                custom_amount = await get_custom_amount(callback.from_user.id, app_context.db_pool)
                logger.info(f"Retrieved custom amount: {custom_amount}")
                
                if custom_amount:
                    # Use saved custom amount directly (in XLM)
                    logger.info(f"Using saved custom amount: {custom_amount} XLM")
                    await handle_quick_buy_xlm(callback, asset_code, asset_issuer, custom_amount, app_context)
                else:
                    # Prompt for custom amount input (in XLM)
                    logger.info("No saved custom amount, prompting for input")
                    await callback.message.reply(
                        f"Enter the amount of XLM to spend:\n\n"
                        f"**Example:** `50` (for 50 XLM)\n\n"
                        f"💡 **Tip:** This XLM amount will be saved for future use with any asset!",
                        parse_mode="Markdown"
                    )
                    await state.set_state(BuySellStates.waiting_for_custom_amount)
            else:
                logger.error("Asset information not found in state")
                await callback.message.reply("❌ Asset information not found. Please try again.")
        
        elif data == "clear_ca":
            # Clear custom amount
            try:
                logger.info(f"Clearing custom amount for user {callback.from_user.id}")
                await clear_custom_amount(callback.from_user.id, app_context.db_pool)
                logger.info(f"Custom amount cleared successfully for user {callback.from_user.id}")
                
                # Send confirmation message
                await callback.message.reply("🗑️ Custom XLM amount cleared! The button will show 'Custom Amount' next time.")
                
                # Refresh the menu to show updated button
                state_data = await state.get_data()
                asset_code = state_data.get('asset_code')
                asset_issuer = state_data.get('asset_issuer')
                
                logger.info(f"Refreshing menu for asset: {asset_code}:{asset_issuer}")
                
                if asset_code and asset_issuer:
                    await refresh_buy_menu(callback, asset_code, asset_issuer, app_context)
                else:
                    logger.error("Asset information not found in state after clearing custom amount")
                    await callback.message.reply("❌ Error: Asset information not found. Please try the buy process again.")
            except Exception as e:
                logger.error(f"Error in clear_ca handler: {str(e)}", exc_info=True)
                await callback.message.reply(f"❌ Error clearing custom amount: {str(e)}")
                # Try to return to main menu as fallback
                try:
                    await state.clear()
                    dynamic_welcome = await get_welcome_text(callback.from_user.id, app_context)
                    await callback.message.edit_text(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")
                except Exception as fallback_error:
                    logger.error(f"Fallback error: {fallback_error}")
                    await callback.message.reply("❌ Error occurred. Please use /start to return to main menu.")
        
        elif data == "rf":
            # Refresh buy menu
            # Get asset info from state
            state_data = await state.get_data()
            asset_code = state_data.get('asset_code')
            asset_issuer = state_data.get('asset_issuer')
            
            if asset_code and asset_issuer:
                await refresh_buy_menu(callback, asset_code, asset_issuer, app_context)
            else:
                await callback.message.reply("❌ Asset information not found. Please try again.")
            
        elif data == "back_to_main":
            # Return to main menu
            await state.clear()
            dynamic_welcome = await get_welcome_text(callback.from_user.id, app_context)
            await callback.message.edit_text(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error in handle_buy_menu_callback: {str(e)}", exc_info=True)
        try:
            await callback.message.reply(f"❌ Error processing menu action: {str(e)}")
        except Exception as reply_error:
            logger.error(f"Could not send error message: {reply_error}")
    finally:
        # Always answer the callback to prevent hanging
        try:
            await callback.answer()
        except Exception as answer_error:
            logger.error(f"Could not answer callback: {answer_error}")

async def handle_quick_buy_xlm(callback: types.CallbackQuery, asset_code: str, asset_issuer: str, xlm_amount: float, app_context):
    """Handle quick buy with XLM amounts using PriceService for accurate calculations"""
    try:
        # Show processing message
        processing_msg = await callback.message.reply("🔄 Processing your buy order...")
        
        # Calculate token amount using PriceService
        estimated_tokens = await app_context.price_service.calculate_tokens_for_xlm(
            asset_code, asset_issuer, xlm_amount
        )
        
        logger.info(f"Quick buy: {xlm_amount} XLM ≈ {estimated_tokens:.7f} {asset_code}")
        
        # Use existing perform_buy function
        response, actual_xlm_spent, actual_amount_received, actual_fee_paid, fee_percentage = await perform_buy(
            callback.from_user.id, app_context.db_pool, asset_code, asset_issuer, estimated_tokens, app_context
        )
        
        # Delete processing message
        await processing_msg.delete()
        
        # Success message
        await callback.message.reply(
            f"✅ **Buy Successful!**\n\n"
            f"• Bought: {actual_amount_received:.7f} {asset_code}\n"
            f"• Spent: {actual_xlm_spent:.7f} XLM\n"
            f"• Fee: {actual_fee_paid:.7f} XLM ({fee_percentage:.2f}%)\n"
            f"• Tx Hash: `{response['hash']}`",
            parse_mode="Markdown"
        )
        
        # Delete the buy menu to clean up
        try:
            await callback.message.delete()
        except Exception as e:
            logger.warning(f"Could not delete buy menu: {e}")
        
        # Return to main menu
        dynamic_welcome = await get_welcome_text(callback.from_user.id, app_context)
        await callback.message.reply(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Quick buy error: {str(e)}", exc_info=True)
        await callback.message.reply(f"❌ Buy failed: {str(e)}")
        
        # Return to main menu on error
        dynamic_welcome = await get_welcome_text(callback.from_user.id, app_context)
        await callback.message.reply(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def refresh_buy_menu(callback: types.CallbackQuery, asset_code: str, asset_issuer: str, app_context):
    """Refresh the buy menu with updated data"""
    try:
        logger.info(f"Refreshing buy menu for user {callback.from_user.id}, asset: {asset_code}:{asset_issuer}")
        
        # Fetch fresh asset info
        asset_info = await app_context.price_service.get_asset_info(asset_code, asset_issuer)
        logger.info(f"Asset info fetched successfully")
        
        # Get updated user balance
        public_key = await app_context.load_public_key(callback.from_user.id)
        account = await load_account_async(public_key, app_context)
        available_xlm = calculate_available_xlm(account)
        logger.info(f"Available XLM: {available_xlm}")
        
        # Create updated menu
        menu_text = create_buy_menu_text(asset_info, asset_code, asset_issuer, available_xlm)
        keyboard = await create_buy_menu_keyboard(asset_code, asset_issuer, available_xlm, callback.from_user.id, app_context)
        logger.info(f"Menu text and keyboard created successfully")
        
        # Update the message
        await callback.message.edit_text(menu_text, reply_markup=keyboard, parse_mode="Markdown")
        logger.info(f"Menu refreshed successfully")
        
    except Exception as e:
        logger.error(f"Error refreshing buy menu: {str(e)}", exc_info=True)
        try:
            await callback.message.reply(f"❌ Failed to refresh menu: {str(e)}")
        except Exception as reply_error:
            logger.error(f"Could not send error message: {reply_error}")
            # Try to return to main menu as fallback
            try:
                await callback.message.edit_text("❌ Error refreshing menu. Use /start to return to main menu.")
            except Exception as edit_error:
                logger.error(f"Could not edit message: {edit_error}")

async def process_custom_amount(message: types.Message, state: FSMContext, app_context):
    """Process custom amount input for buying (XLM amount)"""
    try:
        data = await state.get_data()
        asset_code = data['asset_code']
        asset_issuer = data['asset_issuer']
        xlm_amount = float(message.text)
        
        if xlm_amount <= 0:
            raise ValueError("Amount must be positive")
        
        # Show processing message
        processing_msg = await message.reply("🔄 Processing your buy order...")
        
        # Calculate token amount using PriceService
        estimated_tokens = await app_context.price_service.calculate_tokens_for_xlm(
            asset_code, asset_issuer, xlm_amount
        )
        
        logger.info(f"Custom amount buy: {xlm_amount} XLM ≈ {estimated_tokens:.7f} {asset_code}")
        
        # Use existing perform_buy function with calculated token amount
        response, actual_xlm_spent, actual_amount_received, actual_fee_paid, fee_percentage = await perform_buy(
            message.from_user.id, app_context.db_pool, asset_code, asset_issuer, estimated_tokens, app_context
        )
        
        # Delete processing message
        await processing_msg.delete()
        
        # Save the custom XLM amount for future use
        await save_custom_amount(message.from_user.id, xlm_amount, app_context.db_pool)
        
        # Success message
        await message.reply(
            f"✅ **Buy Successful!**\n\n"
            f"• Bought: {actual_amount_received:.7f} {asset_code}\n"
            f"• Spent: {actual_xlm_spent:.7f} XLM\n"
            f"• Fee: {actual_fee_paid:.7f} XLM ({fee_percentage:.2f}%)\n"
            f"• Tx Hash: `{response['hash']}`\n\n"
            f"💾 **Saved:** {xlm_amount} XLM as your custom amount for future use!",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Custom amount buy error: {str(e)}", exc_info=True)
        await message.reply(f"❌ Buy failed: {str(e)}")
    finally:
        await state.clear()
        dynamic_welcome = await get_welcome_text(message.from_user.id, app_context)
        await message.reply(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def show_sell_asset_selection(message: types.Message, app_context, state: FSMContext, user_id: int = None):
    """Show asset selection menu for selling"""
    try:
        # Get user ID - use provided user_id or fall back to message.from_user.id
        if user_id is None:
            user_id = message.from_user.id
        logger.info(f"show_sell_asset_selection called for user_id: {user_id}")
        
        # Get user's public key and account
        public_key = await app_context.load_public_key(user_id)
        account = await load_account_async(public_key, app_context)
        
        # Get user's assets (excluding XLM)
        balance_lines = [
            {"code": b['asset_code'], "issuer": b['asset_issuer'], "balance": b['balance']}
            for b in account["balances"]
            if b['asset_type'] in ('credit_alphanum4', 'credit_alphanum12') and float(b['balance']) > 0
        ]
        
        if not balance_lines:
            await message.reply(
                "❌ **No Assets to Sell**\n\n"
                "You don't have any assets to sell.\n"
                "Use the Buy function to acquire assets first!",
                parse_mode="Markdown"
            )
            await state.clear()
            return
        
        # Fetch asset values and create selection text
        selection_text = "**Select an asset to sell:**\n\n"
        asset_buttons = []
        
        for asset in balance_lines:
            # Get asset value
            value_in_xlm, value_in_usd = await app_context.price_service.get_asset_value(
                asset['code'], asset['issuer'], asset['balance']
            )
            
            # Format balance display
            balance_display = f"{float(asset['balance']):,.0f}" if float(asset['balance']) >= 1000 else f"{float(asset['balance']):.7f}"
            value_display = f"≈ {value_in_xlm:.4f} XLM"
            if value_in_usd > 0:
                value_display += f" (${value_in_usd:.2f})"
            
            selection_text += f"**{asset['code']}** — {balance_display} {value_display}\n"
            
            # Create button for this asset - use shorter callback data
            asset_buttons.append([
                InlineKeyboardButton(
                    text=f"{asset['code']} ({balance_display})",
                    callback_data=f"sa:{asset['code']}"
                )
            ])
        
        # Store asset info in state for later use
        asset_info_dict = {asset['code']: asset['issuer'] for asset in balance_lines}
        await state.update_data(asset_selection=asset_info_dict)
        
        # Add navigation buttons
        asset_buttons.append([
            InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_main")
        ])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=asset_buttons)
        
        await message.reply(selection_text, reply_markup=keyboard, parse_mode="Markdown")
        await state.set_state(BuySellStates.sell_asset_selection)
        
    except Exception as e:
        logger.error(f"Error showing sell asset selection: {str(e)}", exc_info=True)
        await message.reply(f"❌ Error loading assets: {str(e)}")
        await state.clear()

async def show_sell_menu(message: types.Message, asset_code: str, asset_issuer: str, app_context, state: FSMContext, user_id: int = None):
    """Show the enhanced sell menu with asset info and percentage sell options"""
    
    # Get user ID - use provided user_id or fall back to message.from_user.id
    if user_id is None:
        user_id = message.from_user.id
    logger.info(f"show_sell_menu called for user_id: {user_id}")
    
    # Fetch asset information from Stellar Expert using PriceService
    asset_info = await app_context.price_service.get_asset_info(asset_code, asset_issuer)
    
    # Get user's asset balance
    public_key = await app_context.load_public_key(user_id)
    account = await load_account_async(public_key, app_context)
    
    # Find the specific asset balance
    asset_balance = 0
    for balance in account["balances"]:
        if (balance.get('asset_code') == asset_code and 
            balance.get('asset_issuer') == asset_issuer):
            asset_balance = float(balance['balance'])
            break
    
    if asset_balance <= 0:
        await message.reply(f"❌ You don't have any {asset_code} to sell.")
        await state.clear()
        return
    
    # Get asset value
    value_in_xlm, value_in_usd = await app_context.price_service.get_asset_value(
        asset_code, asset_issuer, asset_balance
    )
    
    logger.info(f"Creating sell menu for {asset_code}:{asset_issuer}, balance: {asset_balance}")
    
    # Create menu text
    menu_text = create_sell_menu_text(asset_info, asset_code, asset_issuer, asset_balance, value_in_xlm, value_in_usd)
    
    # Create keyboard with percentage options
    keyboard = create_sell_menu_keyboard(asset_code, asset_issuer, asset_balance)
    
    # Delete the asset selection menu to clean up the chat
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Could not delete asset selection menu: {e}")
    
    # Use send_message instead of reply since we deleted the original message
    await message.bot.send_message(message.chat.id, menu_text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(BuySellStates.sell_menu_displayed)

def create_sell_menu_text(asset_info, asset_code: str, asset_issuer: str, asset_balance: float, value_in_xlm: float, value_in_usd: float) -> str:
    """Create the text for the sell menu using PriceService data"""
    
    text = f"**Sell {asset_code}** — ({asset_info.get('name', asset_code) if asset_info else asset_code})\n"
    text += f"`{asset_issuer}`\n\n"
    
    # Asset balance and value
    balance_display = f"{asset_balance:,.0f}" if asset_balance >= 1000 else f"{asset_balance:.7f}"
    text += f"**💰 Your Balance:** {balance_display} {asset_code}\n"
    text += f"**💎 Value:** ≈ {value_in_xlm:.4f} XLM"
    if value_in_usd > 0:
        text += f" (${value_in_usd:.2f})"
    text += "\n\n"
    
    if asset_info:
        # Market data section using PriceService data
        text += "**📊 Market Data:**\n"
        if asset_info.get('price_usd'):
            text += f"• Price: ${asset_info['price_usd']:.8f}\n"
        if asset_info.get('price_xlm'):
            text += f"• Price: {asset_info['price_xlm']:.8f} XLM\n"
        if asset_info.get('market_cap_usd'):
            text += f"• Market Cap: ${asset_info['market_cap_usd']:,.0f}\n"
        if asset_info.get('volume_24h'):
            text += f"• 24h Volume: ${asset_info['volume_24h']:,.0f}\n"
        if asset_info.get('supply'):
            text += f"• Supply: {asset_info['supply']:,.0f}\n"
        
        if asset_info.get('domain'):
            text += f"• Domain: {asset_info['domain']}\n"
        if asset_info.get('tags'):
            text += f"• Tags: {', '.join(asset_info['tags'])}\n"
    
    text += "\n**Select percentage to sell:**"
    
    return text

def create_sell_menu_keyboard(asset_code: str, asset_issuer: str, asset_balance: float) -> InlineKeyboardMarkup:
    """Create the keyboard for the sell menu with percentage options"""
    
    # Percentage options (10%, 25%, 50%, 100%)
    percentages = [10, 25, 50, 100]
    
    logger.info(f"Creating sell keyboard with percentages: {percentages}, balance: {asset_balance}")
    
    # Create keyboard rows
    keyboard_rows = []
    
    # Percentage buttons (2 per row for better visibility)
    for i in range(0, len(percentages), 2):
        row = []
        for j in range(2):
            if i + j < len(percentages):
                percentage = percentages[i + j]
                button_text = f"{percentage}%"
                
                # Use shorter callback data to avoid BUTTON_DATA_INVALID error
                row.append(InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"sell_pct:{percentage}"
                ))
                logger.info(f"Creating sell button: {button_text}")
        keyboard_rows.append(row)
    
    # Custom percentage button
    keyboard_rows.append([
        InlineKeyboardButton(text="💰 Custom %", callback_data="sell_custom_pct")
    ])
    
    # Navigation buttons
    keyboard_rows.extend([
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="sell_refresh")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="sell_back_to_assets")]
    ])
    
    logger.info(f"Created sell keyboard with {len(keyboard_rows)} rows")
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

async def handle_sell_menu_callback(callback: types.CallbackQuery, state: FSMContext, app_context):
    """Handle callbacks from the sell menu"""
    data = callback.data
    
    if data.startswith("sell_pct:"):
        # Handle percentage-based sell
        _, percentage = data.split(":")
        # Get asset info from state
        state_data = await state.get_data()
        asset_code = state_data.get('asset_code')
        asset_issuer = state_data.get('asset_issuer')
        
        if asset_code and asset_issuer:
            await handle_percentage_sell(callback, asset_code, asset_issuer, int(percentage), app_context)
        else:
            await callback.message.reply("❌ Asset information not found. Please try again.")
    
    elif data == "sell_custom_pct":
        # Handle custom percentage input
        logger.info(f"Custom percentage button clicked for user {callback.from_user.id}")
        
        # Get asset info from state
        state_data = await state.get_data()
        asset_code = state_data.get('asset_code')
        asset_issuer = state_data.get('asset_issuer')
        
        logger.info(f"Asset info from state: {asset_code}:{asset_issuer}")
        
        if asset_code and asset_issuer:
            await callback.message.reply(
                f"Enter the percentage of {asset_code} to sell:\n\n"
                f"**Example:** `25` (for 25%)\n\n"
                f"💡 **Tip:** Enter a number between 1 and 100!",
                parse_mode="Markdown"
            )
            await state.set_state(BuySellStates.waiting_for_sell_percentage)
        else:
            logger.error("Asset information not found in state")
            await callback.message.reply("❌ Asset information not found. Please try again.")
    
    elif data == "sell_refresh":
        # Refresh sell menu
        # Get asset info from state
        state_data = await state.get_data()
        asset_code = state_data.get('asset_code')
        asset_issuer = state_data.get('asset_issuer')
        
        if asset_code and asset_issuer:
            await refresh_sell_menu(callback, asset_code, asset_issuer, app_context, callback.from_user.id)
        else:
            await callback.message.reply("❌ Asset information not found. Please try again.")
    
    elif data == "sell_back_to_assets":
        # Return to asset selection
        await show_sell_asset_selection(callback.message, app_context, state)
    
    await callback.answer()

async def handle_percentage_sell(callback: types.CallbackQuery, asset_code: str, asset_issuer: str, percentage: int, app_context):
    """Handle percentage-based sell operations"""
    try:
        # Get user's current asset balance
        user_id = callback.from_user.id
        logger.info(f"handle_percentage_sell called for user_id: {user_id}")
        
        public_key = await app_context.load_public_key(user_id)
        account = await load_account_async(public_key, app_context)
        
        # Find the specific asset balance
        asset_balance = 0
        for balance in account["balances"]:
            if (balance.get('asset_code') == asset_code and 
                balance.get('asset_issuer') == asset_issuer):
                asset_balance = float(balance['balance'])
                break
        
        if asset_balance <= 0:
            await callback.message.reply(f"❌ You don't have any {asset_code} to sell.")
            return
        
        # Calculate amount to sell based on percentage
        amount_to_sell = asset_balance * (percentage / 100)
        
        # Show processing message
        processing_msg = await callback.message.reply(f"🔄 Processing your {percentage}% sell order...")
        
        logger.info(f"Percentage sell: {percentage}% of {asset_balance} {asset_code} = {amount_to_sell:.7f} {asset_code}")
        
        # Use existing perform_sell function
        response, actual_xlm_received, actual_amount_sent, actual_fee_paid, fee_percentage = await perform_sell(
            user_id, app_context.db_pool, asset_code, asset_issuer, amount_to_sell, app_context
        )
        
        # Delete processing message
        await processing_msg.delete()
        
        # Success message
        await callback.message.reply(
            f"✅ **Sell Successful!**\n\n"
            f"• Sold: {actual_amount_sent:.7f} {asset_code} ({percentage}%)\n"
            f"• Received: {actual_xlm_received:.7f} XLM\n"
            f"• Fee: {actual_fee_paid:.7f} XLM ({fee_percentage:.2f}%)\n"
            f"• Tx Hash: `{response['hash']}`",
            parse_mode="Markdown"
        )
        
        # Delete the sell menu to clean up
        try:
            await callback.message.delete()
        except Exception as e:
            # Suppress cleanup errors - they don't affect functionality
            logger.debug(f"Could not delete sell menu (non-critical): {e}")
        
        # Return to main menu
        try:
            dynamic_welcome = await get_welcome_text(user_id, app_context)
            await callback.message.bot.send_message(callback.message.chat.id, dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error returning to main menu: {e}")
            # Fallback: just send a simple message
            await callback.message.bot.send_message(callback.message.chat.id, "✅ Sell completed! Use /start to return to main menu.")
        
    except Exception as e:
        logger.error(f"Percentage sell error: {str(e)}", exc_info=True)
        try:
            await callback.message.bot.send_message(callback.message.chat.id, f"❌ Sell failed: {str(e)}")
        except Exception as send_error:
            logger.error(f"Could not send error message: {send_error}")
        
        # Return to main menu on error
        try:
            dynamic_welcome = await get_welcome_text(callback.from_user.id, app_context)
            await callback.message.bot.send_message(callback.message.chat.id, dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")
        except Exception as menu_error:
            logger.error(f"Error returning to main menu: {menu_error}")
            await callback.message.bot.send_message(callback.message.chat.id, "✅ Use /start to return to main menu.")

async def refresh_sell_menu(callback: types.CallbackQuery, asset_code: str, asset_issuer: str, app_context, user_id: int = None):
    """Refresh the sell menu with updated data"""
    try:
        # Get user ID - use provided user_id or fall back to callback.from_user.id
        if user_id is None:
            user_id = callback.from_user.id
        logger.info(f"refresh_sell_menu called for user_id: {user_id}")
        
        # Fetch fresh asset info
        asset_info = await app_context.price_service.get_asset_info(asset_code, asset_issuer)
        
        # Get updated user balance
        public_key = await app_context.load_public_key(user_id)
        account = await load_account_async(public_key, app_context)
        
        # Find the specific asset balance
        asset_balance = 0
        for balance in account["balances"]:
            if (balance.get('asset_code') == asset_code and 
                balance.get('asset_issuer') == asset_issuer):
                asset_balance = float(balance['balance'])
                break
        
        if asset_balance <= 0:
            await callback.message.edit_text("❌ You no longer have any of this asset to sell.")
            return
        
        # Get updated asset value
        value_in_xlm, value_in_usd = await app_context.price_service.get_asset_value(
            asset_code, asset_issuer, asset_balance
        )
        
        # Create updated menu
        menu_text = create_sell_menu_text(asset_info, asset_code, asset_issuer, asset_balance, value_in_xlm, value_in_usd)
        keyboard = create_sell_menu_keyboard(asset_code, asset_issuer, asset_balance)
        
        # Update the message
        await callback.message.edit_text(menu_text, reply_markup=keyboard, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error refreshing sell menu: {str(e)}")
        await callback.message.reply(f"❌ Failed to refresh menu: {str(e)}")

async def process_custom_sell_percentage(message: types.Message, state: FSMContext, app_context):
    """Process custom percentage input for selling"""
    try:
        data = await state.get_data()
        asset_code = data['asset_code']
        asset_issuer = data['asset_issuer']
        percentage = float(message.text)
        
        if percentage <= 0 or percentage > 100:
            raise ValueError("Percentage must be between 1 and 100")
        
        # Get user's current asset balance
        public_key = await app_context.load_public_key(message.from_user.id)
        account = await load_account_async(public_key, app_context)
        
        # Find the specific asset balance
        asset_balance = 0
        for balance in account["balances"]:
            if (balance.get('asset_code') == asset_code and 
                balance.get('asset_issuer') == asset_issuer):
                asset_balance = float(balance['balance'])
                break
        
        if asset_balance <= 0:
            await message.reply(f"❌ You don't have any {asset_code} to sell.")
            await state.clear()
            return
        
        # Calculate amount to sell based on percentage
        amount_to_sell = asset_balance * (percentage / 100)
        
        # Show processing message
        processing_msg = await message.reply(f"🔄 Processing your {percentage}% sell order...")
        
        logger.info(f"Custom percentage sell: {percentage}% of {asset_balance} {asset_code} = {amount_to_sell:.7f} {asset_code}")
        
        # Use existing perform_sell function
        response, actual_xlm_received, actual_amount_sent, actual_fee_paid, fee_percentage = await perform_sell(
            message.from_user.id, app_context.db_pool, asset_code, asset_issuer, amount_to_sell, app_context
        )
        
        # Delete processing message
        await processing_msg.delete()
        
        # Success message
        await message.reply(
            f"✅ **Sell Successful!**\n\n"
            f"• Sold: {actual_amount_sent:.7f} {asset_code} ({percentage}%)\n"
            f"• Received: {actual_xlm_received:.7f} XLM\n"
            f"• Fee: {actual_fee_paid:.7f} XLM ({fee_percentage:.2f}%)\n"
            f"• Tx Hash: `{response['hash']}`",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Custom percentage sell error: {str(e)}", exc_info=True)
        try:
            await message.bot.send_message(message.chat.id, f"❌ Sell failed: {str(e)}")
        except Exception as send_error:
            logger.error(f"Could not send error message: {send_error}")
        
        # Return to main menu on error
        try:
            dynamic_welcome = await get_welcome_text(message.from_user.id, app_context)
            await message.bot.send_message(message.chat.id, dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")
        except Exception as menu_error:
            logger.error(f"Error returning to main menu: {menu_error}")
            await message.bot.send_message(message.chat.id, "✅ Use /start to return to main menu.")
    finally:
        await state.clear()
        dynamic_welcome = await get_welcome_text(message.from_user.id, app_context)
        await message.reply(dynamic_welcome, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def handle_sell_asset_selection(callback: types.CallbackQuery, state: FSMContext, app_context):
    """Handle asset selection for selling"""
    try:
        # Parse asset code from callback data
        _, asset_code = callback.data.split(":")
        
        logger.info(f"Asset selected for selling: {asset_code}")
        
        # Get asset info from state
        state_data = await state.get_data()
        asset_selection = state_data.get('asset_selection', {})
        
        if asset_code not in asset_selection:
            await callback.message.reply("❌ Asset information not found. Please try again.")
            return
        
        asset_issuer = asset_selection[asset_code]
        logger.info(f"Found asset issuer: {asset_issuer}")
        
        # Store asset info in state
        await state.update_data(asset_code=asset_code, asset_issuer=asset_issuer)
        
        # Show sell menu for selected asset
        await show_sell_menu(callback.message, asset_code, asset_issuer, app_context, state, callback.from_user.id)
        
    except Exception as e:
        logger.error(f"Error handling sell asset selection: {str(e)}", exc_info=True)
        await callback.message.reply(f"❌ Error selecting asset: {str(e)}")
        await state.clear()

async def buy_command(message: types.Message, state: FSMContext, app_context):
    """Handle /buy command - trigger enhanced buy flow"""
    logger.info(f"Buy command triggered by user {message.from_user.id}")
    
    # Set action to buy and trigger asset input
    await state.update_data(action='buy')
    await message.reply(
        "Please enter the asset code and issuer for buying in the format: `code:issuer`\n\n"
        "**Example:** `USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN`",
        parse_mode="Markdown"
    )
    await state.set_state(BuySellStates.waiting_for_asset)

async def sell_command(message: types.Message, state: FSMContext, app_context):
    """Handle /sell command - trigger enhanced sell flow"""
    logger.info(f"Sell command triggered by user {message.from_user.id}")
    
    # Set action to sell and show asset selection
    await state.update_data(action='sell')
    await show_sell_asset_selection(message, app_context, state, message.from_user.id)

async def help_command(message: types.Message):
    """Handle /help command - show enhanced trading features"""
    help_text = """
🤖 **PhotonBot Enhanced Trading Commands**

## 🚀 **Quick Trading Commands:**
• **`/buy`** - Enhanced buy with XLM amounts & market data
• **`/sell`** - Enhanced sell with asset selection & percentages
• **`/balance`** - Check wallet balance & asset values

## 💰 **Enhanced Buy Features:**
• **Market Data**: Real-time prices, market cap, volume
• **Quick Buy**: 25, 50, 100, 200 XLM buttons
• **Custom Amount**: Save your preferred XLM amount
• **Price Calculations**: Real-time token estimates

## 📈 **Enhanced Sell Features:**
• **Asset Selection**: See all your assets with values
• **Percentage Selling**: 10%, 25%, 50%, 100% options
• **Custom Percentage**: Enter any percentage (1-100%)
• **Market Info**: Asset details and market data

## 🎯 **How to Use:**

### **Buy Assets:**
1. Type `/buy` or click Buy button
2. Enter asset: `USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN`
3. Choose XLM amount or custom amount
4. Confirm trade

### **Sell Assets:**
1. Type `/sell` or click Sell button
2. Select asset from your portfolio
3. Choose percentage or custom amount
4. Confirm trade

## 🔧 **Other Commands:**
• **`/register`** - Create new Turnkey wallet
• **`/login`** - Access existing wallet
• **`/start`** - Show main menu

## 💡 **Tips:**
• Custom amounts are saved for future use
• Market data updates in real-time
• All trades use advanced pathfinding
• Clean, professional interface

**Happy Trading! 🚀**
"""
    
    await message.reply(help_text, parse_mode="Markdown")
