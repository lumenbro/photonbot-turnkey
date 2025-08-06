# referrals.py (modified for Turnkey signing in payouts)
import logging
import csv
import os
from datetime import datetime, timedelta
from stellar_sdk import Asset, Payment
from core.stellar import load_account_async, build_and_submit_transaction

logger = logging.getLogger(__name__)

# Hardcode testnet network passphrase
NETWORK_PASSPHRASE = "Public Global Stellar Network ; September 2015"

async def log_xlm_volume(user_id, xlm_volume, tx_hash=None, db_pool=None):
    async with db_pool.acquire() as conn:
        if tx_hash:
            exists = await conn.fetchval(
                "SELECT COUNT(*) FROM trades WHERE tx_hash = $1", tx_hash
            )
            if exists > 0:
                logger.warning(f"Transaction {tx_hash} already logged, skipping")
                return
        await conn.execute(
            "INSERT INTO trades (user_id, xlm_volume, tx_hash) VALUES ($1, $2, $3)",
            user_id, xlm_volume, tx_hash
        )
    logger.info(f"Logged XLM volume for user {user_id}: {xlm_volume} XLM, tx_hash: {tx_hash}")

async def calculate_referral_shares(db_pool, user_id, fee):
    async with db_pool.acquire() as conn:
        # Get the referrer chain (up to 5 levels)
        referrer_chain = []
        current_user = user_id
        for _ in range(5):  # Up to 5 levels of referrals
            referrer = await conn.fetchval(
                "SELECT referrer_id FROM referrals WHERE referee_id = $1",
                current_user
            )
            if not referrer:
                break
            referrer_chain.append(referrer)
            current_user = referrer
        logger.info(f"Referrer chain for user {user_id}: {referrer_chain}")

        # Calculate the referrer's trading volume for the past week
        one_week_ago = datetime.utcnow() - timedelta(days=7)
        user_volume = await conn.fetchval(
            "SELECT SUM(xlm_volume) FROM trades WHERE user_id = $1 AND timestamp >= $2",
            user_id, one_week_ago
        ) or 0
        logger.info(f"User {user_id} trading volume (past week): {user_volume} XLM")

        # Determine the share percentage based on volume
        share_percentage = 0.35 if user_volume >= 100000 else 0.25  # $10,000 in XLM (assuming 1 XLM = $0.10)
        logger.info(f"Share percentage for user {user_id}: {share_percentage}")

        # Distribute shares across the referrer chain
        for level, referrer_id in enumerate(referrer_chain, 1):
            level_share = share_percentage * (1 - 0.05 * (level - 1))  # Decrease by 5% per level
            logger.info(f"Level {level} share for referrer {referrer_id}: {level_share}")
            if level_share <= 0:
                logger.warning(f"Level share for referrer {referrer_id} at level {level} is <= 0, skipping")
                break
            amount = fee * level_share
            logger.info(f"Calculated referral amount for referrer {referrer_id} at level {level}: {amount} XLM")
            try:
                await conn.execute(
                    "INSERT INTO rewards (user_id, amount) VALUES ($1, $2)",
                    referrer_id, amount
                )
                logger.info(f"Successfully logged referral fee for referrer {referrer_id}: {amount} XLM")
            except Exception as e:
                logger.error(f"Failed to log referral fee for referrer {referrer_id}: {str(e)}", exc_info=True)

async def export_unpaid_rewards(db_pool, output_file):
    """
    Export unpaid rewards to a CSV file if the total amount per user is >= 0.1 XLM.
    
    Args:
        db_pool: Single database pool for the merged database.
        output_file: Path to the output CSV file.
    
    Returns:
        Tuple of (exported_file_path, total_payout, payout_list):
        - exported_file_path: Path to the CSV file, or None if no rewards to export.
        - total_payout: Total amount to be paid out (float).
        - payout_list: List of (user_id, public_key, amount) tuples for the payout.
    """
    async with db_pool.acquire() as conn:
        rewards = await conn.fetch("""
            SELECT user_id, SUM(amount) AS total_amount
            FROM rewards
            WHERE status = 'unpaid'
            GROUP BY user_id
            HAVING SUM(amount) >= 0.1  -- Minimum payout threshold of 0.1 XLM
        """)

        if not rewards:
            logger.info("No unpaid rewards found to export.")
            return None, 0, []

        # Calculate total payout amount and prepare payout list
        total_payout = 0
        payout_list = []
        for row in rewards:
            user_id = row['user_id']
            amount = float(row['total_amount'])  # Convert Decimal to float
            public_key = await conn.fetchval(
                "SELECT public_key FROM users WHERE telegram_id = $1", user_id
            )
            if public_key:
                total_payout += amount
                payout_list.append((user_id, public_key, amount))
            else:
                logger.warning(f"No public key found for user {user_id}, skipping payout")

        # Export to CSV for record-keeping
        with open(output_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['user_id', 'public_key', 'amount'])
            for user_id, public_key, amount in payout_list:
                writer.writerow([user_id, public_key, amount])

        logger.info(f"Exported unpaid rewards to {output_file} with total payout {total_payout:.7f} XLM")
        return output_file, total_payout, payout_list

async def daily_payout(db_pool, bot, chat_id, app_context):
    output_file = f"referral_rewards_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    exported_file, total_payout, payout_list = await export_unpaid_rewards(db_pool, output_file)

    if not exported_file:
        if chat_id:
            await bot.send_message(chat_id, "No unpaid rewards to export.")
        return

    # Check the balance of DISBURSEMENT_WALLET (used for payouts)
    try:
        disbursement_public_key = os.getenv("DISBURSEMENT_WALLET")
        fee_account = await load_account_async(disbursement_public_key, app_context)
        fee_balance = float(next((b["balance"] for b in fee_account["balances"] if b["asset_type"] == "native"), "0"))
    except Exception as e:
        logger.error(f"Failed to fetch DISBURSEMENT_WALLET balance: {str(e)}")
        if chat_id:
            await bot.send_message(chat_id, f"Failed to fetch DISBURSEMENT_WALLET balance: {str(e)}")
        return

    if fee_balance < total_payout:
        logger.error(f"Insufficient balance in DISBURSEMENT_WALLET: {fee_balance} XLM available, {total_payout} XLM required")
        if chat_id:
            await bot.send_message(chat_id, f"Insufficient balance in DISBURSEMENT_WALLET: {fee_balance} XLM available, {total_payout} XLM required")
        return

    fee_telegram_id = getattr(app_context, 'fee_telegram_id', -1)

    async with db_pool.acquire() as conn:
        successful_payouts = 0
        failed_payouts = 0
        operations = []
        batch_size = 100

        for i, (user_id, public_key, amount) in enumerate(payout_list):
            rounded_amount = round(amount, 7)
            operations.append(Payment(
                destination=public_key,
                asset=Asset.native(),
                amount=str(rounded_amount)
            ))

            if len(operations) == batch_size or i == len(payout_list) - 1:
                try:
                    response, _ = await build_and_submit_transaction(
                        fee_telegram_id,
                        db_pool,
                        operations,
                        app_context,
                        memo="Referral Payout"
                    )
                    for user_id, _, _ in payout_list[i - len(operations) + 1:i + 1]:
                        await conn.execute(
                            "UPDATE rewards SET status = 'paid', paid_at = CURRENT_TIMESTAMP WHERE user_id = $1 AND status = 'unpaid'",
                            user_id
                        )
                    successful_payouts += len(operations)
                    logger.info(f"Batch payout successful: {response['hash']}")
                except Exception as e:
                    logger.error(f"Batch payout failed: {str(e)}")
                    failed_payouts += len(operations)
                operations = []

    if chat_id:
        message = (
            f"Referral rewards payout completed.\n"
            f"Exported to {exported_file}\n"
            f"Total Payout: {total_payout:.7f} XLM\n"
            f"Successful Payouts: {successful_payouts}\n"
            f"Failed Payouts: {failed_payouts}\n"
            f"Disbursement Wallet Balance After Payout: {fee_balance - total_payout:.7f} XLM"
        )
        await bot.send_message(chat_id, message)