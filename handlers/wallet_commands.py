import logging
from aiogram import types
from aiogram.fsm.context import FSMContext
from services.wallet_manager import WalletManager

logger = logging.getLogger(__name__)

async def list_wallets_command(message: types.Message, app_context):
    """Show all wallets for the user"""
    telegram_id = message.from_user.id
    wallet_manager = WalletManager(app_context.db_pool)
    
    try:
        wallets = await wallet_manager.get_all_wallets(telegram_id)
        
        if not wallets:
            await message.reply("❌ No wallets found. Please register a wallet first.")
            return
        
        # Check if legacy user
        is_legacy = await wallet_manager.is_legacy_user(telegram_id)
        
        wallet_list = "💼 **Your Wallets:**\n\n"
        
        for wallet in wallets:
            status = "🟢 Active" if wallet['active'] else "⚪ Inactive"
            wallet_list += f"• `{wallet['public_key']}`\n"
            wallet_list += f"  └ {wallet['description']} - {status}\n\n"
        
        if is_legacy:
            wallet_list += "ℹ️ **Legacy User Note:** You cannot switch between wallets for security reasons.\n"
            wallet_list += "Your legacy wallet is export-only for fund recovery."
        else:
            wallet_list += "💡 **Tip:** Use /switch_wallet to change your active wallet."
        
        await message.reply(wallet_list, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error listing wallets for user {telegram_id}: {e}")
        await message.reply("❌ Error loading wallets. Please try again.")

async def switch_wallet_command(message: types.Message, app_context):
    """Switch active wallet (for new users only)"""
    telegram_id = message.from_user.id
    wallet_manager = WalletManager(app_context.db_pool)
    
    try:
        # Check if legacy user
        is_legacy = await wallet_manager.is_legacy_user(telegram_id)
        
        if is_legacy:
            await message.reply(
                "❌ **Legacy users cannot switch wallets**\n\n"
                "For security reasons, legacy migrated users are restricted to their new Turnkey wallet.\n"
                "Your old wallet is available for export only.\n\n"
                "Use /export_wallet to access your legacy wallet for fund recovery."
            )
            return
        
        # Get all wallets for new user
        wallets = await wallet_manager.get_all_wallets(telegram_id)
        
        if len(wallets) <= 1:
            await message.reply(
                "❌ **No other wallets available**\n\n"
                "You only have one wallet. Create additional wallets through the registration process."
            )
            return
        
        # Create keyboard with wallet options
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        
        keyboard = []
        for wallet in wallets:
            if not wallet['active']:  # Only show inactive wallets as switch options
                keyboard.append([
                    InlineKeyboardButton(
                        text=f"Switch to: {wallet['public_key'][:10]}...",
                        callback_data=f"switch_to_{wallet['public_key']}"
                    )
                ])
        
        if not keyboard:
            await message.reply("❌ No other wallets available to switch to.")
            return
        
        keyboard.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_switch")])
        
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
        
        await message.reply(
            "🔄 **Switch Active Wallet**\n\n"
            "Select a wallet to make active:",
            reply_markup=markup
        )
        
    except Exception as e:
        logger.error(f"Error switching wallet for user {telegram_id}: {e}")
        await message.reply("❌ Error switching wallet. Please try again.")

async def switch_wallet_callback(callback: types.CallbackQuery, app_context):
    """Handle wallet switching callback"""
    telegram_id = callback.from_user.id
    wallet_manager = WalletManager(app_context.db_pool)
    
    try:
        if callback.data == "cancel_switch":
            await callback.message.delete()
            await callback.answer("❌ Wallet switch cancelled.")
            return
        
        if callback.data.startswith("switch_to_"):
            target_public_key = callback.data.replace("switch_to_", "")
            
            # Attempt to switch wallet
            success = await wallet_manager.switch_wallet(telegram_id, target_public_key)
            
            if success:
                await callback.message.edit_text(
                    f"✅ **Wallet switched successfully!**\n\n"
                    f"Active wallet: `{target_public_key}`\n\n"
                    f"Your new wallet is now active for trading."
                )
                await callback.answer("Wallet switched!")
            else:
                await callback.message.edit_text(
                    "❌ **Failed to switch wallet**\n\n"
                    "Please try again or contact support if the issue persists."
                )
                await callback.answer("Switch failed!")
        
    except Exception as e:
        logger.error(f"Error in switch wallet callback for user {telegram_id}: {e}")
        await callback.message.edit_text("❌ Error switching wallet. Please try again.")
        await callback.answer("Error occurred!")

async def wallet_info_command(message: types.Message, app_context):
    """Show detailed information about user's wallets"""
    telegram_id = message.from_user.id
    wallet_manager = WalletManager(app_context.db_pool)
    
    try:
        active_wallet = await wallet_manager.get_active_wallet(telegram_id)
        
        if not active_wallet:
            await message.reply("❌ No active wallet found. Please register a wallet first.")
            return
        
        # Get detailed wallet info
        wallet_info = await wallet_manager.get_wallet_info(telegram_id, active_wallet)
        
        if not wallet_info:
            await message.reply("❌ Wallet information not found.")
            return
        
        info_text = f"💼 **Wallet Information**\n\n"
        info_text += f"**Public Key:** `{wallet_info['public_key']}`\n"
        info_text += f"**Type:** {wallet_info['type'].title()}\n"
        info_text += f"**Status:** {'🟢 Active' if wallet_info['active'] else '⚪ Inactive'}\n"
        info_text += f"**Description:** {wallet_info['description']}\n"
        
        if wallet_info['type'] == 'legacy':
            info_text += "\n⚠️ **Legacy Wallet**\n"
            info_text += "This is your old wallet for export only.\n"
            info_text += "Use /export_wallet to access your funds."
        elif wallet_info['type'] == 'current':
            info_text += "\n✅ **Current Trading Wallet**\n"
            info_text += "This is your active wallet for trading."
        else:  # turnkey
            info_text += f"\n🔧 **Can Switch:** {'Yes' if wallet_info['can_switch'] else 'No'}"
        
        await message.reply(info_text, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error getting wallet info for user {telegram_id}: {e}")
        await message.reply("❌ Error loading wallet information. Please try again.")

def register_wallet_commands(dp, app_context):
    """Register wallet management commands"""
    dp.message.register(list_wallets_command, commands=["wallets"])
    dp.message.register(switch_wallet_command, commands=["switch_wallet"])
    dp.message.register(wallet_info_command, commands=["wallet_info"])
    dp.callback_query.register(switch_wallet_callback, lambda c: c.data.startswith("switch_to_") or c.data == "cancel_switch")
