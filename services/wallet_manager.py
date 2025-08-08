import logging
import asyncpg
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

class WalletManager:
    def __init__(self, db_pool):
        self.db_pool = db_pool
    
    async def get_active_wallet(self, telegram_id: int) -> Optional[str]:
        """
        Get the active wallet for any user (legacy or new)
        
        Args:
            telegram_id: The user's Telegram ID
            
        Returns:
            The public key of the active wallet, or None if no wallet found
        """
        async with self.db_pool.acquire() as conn:
            # Check if legacy user first
            legacy_user = await conn.fetchrow("""
                SELECT public_key FROM users 
                WHERE telegram_id = $1 AND source_old_db IS NOT NULL
            """, telegram_id)
            
            if legacy_user:
                logger.info(f"Legacy user {telegram_id} using wallet: {legacy_user['public_key']}")
                return legacy_user['public_key']  # Legacy users use users.public_key
            
            # For new users, get from turnkey_wallets
            active_wallet = await conn.fetchrow("""
                SELECT public_key FROM turnkey_wallets 
                WHERE telegram_id = $1 AND is_active = TRUE
            """, telegram_id)
            
            if active_wallet:
                logger.info(f"New user {telegram_id} using active wallet: {active_wallet['public_key']}")
                return active_wallet['public_key']
            
            logger.warning(f"No active wallet found for user {telegram_id}")
            return None
    
    async def get_all_wallets(self, telegram_id: int) -> List[Dict]:
        """
        Get all wallets for a user (legacy or new)
        
        Args:
            telegram_id: The user's Telegram ID
            
        Returns:
            List of wallet dictionaries with public_key, type, and active status
        """
        async with self.db_pool.acquire() as conn:
            # Check if legacy user
            legacy_user = await conn.fetchrow("""
                SELECT public_key, legacy_public_key FROM users 
                WHERE telegram_id = $1 AND source_old_db IS NOT NULL
            """, telegram_id)
            
            if legacy_user:
                wallets = []
                # Current wallet (new Turnkey wallet)
                if legacy_user['public_key']:
                    wallets.append({
                        'public_key': legacy_user['public_key'],
                        'type': 'current',
                        'active': True,
                        'description': 'New Turnkey Wallet'
                    })
                # Legacy wallet (old wallet for export)
                if legacy_user['legacy_public_key']:
                    wallets.append({
                        'public_key': legacy_user['legacy_public_key'],
                        'type': 'legacy',
                        'active': False,
                        'description': 'Legacy Wallet (Export Only)'
                    })
                return wallets
            
            # For new users, get from turnkey_wallets
            wallet_rows = await conn.fetch("""
                SELECT public_key, is_active, created_at FROM turnkey_wallets 
                WHERE telegram_id = $1 ORDER BY created_at
            """, telegram_id)
            
            wallets = []
            for row in wallet_rows:
                wallets.append({
                    'public_key': row['public_key'],
                    'type': 'turnkey',
                    'active': row['is_active'],
                    'description': f"Turnkey Wallet (Created: {row['created_at'].strftime('%Y-%m-%d')})"
                })
            
            return wallets
    
    async def is_legacy_user(self, telegram_id: int) -> bool:
        """
        Check if a user is a legacy migrated user
        
        Args:
            telegram_id: The user's Telegram ID
            
        Returns:
            True if legacy user, False otherwise
        """
        async with self.db_pool.acquire() as conn:
            legacy_user = await conn.fetchrow("""
                SELECT 1 FROM users 
                WHERE telegram_id = $1 AND source_old_db IS NOT NULL
            """, telegram_id)
            
            return legacy_user is not None
    
    async def switch_wallet(self, telegram_id: int, target_public_key: str) -> bool:
        """
        Switch active wallet for new users (not available for legacy users)
        
        Args:
            telegram_id: The user's Telegram ID
            target_public_key: The public key to switch to
            
        Returns:
            True if successful, False otherwise
        """
        # Legacy users cannot switch wallets
        if await self.is_legacy_user(telegram_id):
            logger.warning(f"Legacy user {telegram_id} attempted to switch wallet - not allowed")
            return False
        
        async with self.db_pool.acquire() as conn:
            # Deactivate all wallets for this user
            await conn.execute("""
                UPDATE turnkey_wallets 
                SET is_active = FALSE 
                WHERE telegram_id = $1
            """, telegram_id)
            
            # Activate the target wallet
            result = await conn.execute("""
                UPDATE turnkey_wallets 
                SET is_active = TRUE 
                WHERE telegram_id = $1 AND public_key = $2
            """, telegram_id, target_public_key)
            
            if result == "UPDATE 1":
                logger.info(f"Successfully switched wallet for user {telegram_id} to {target_public_key}")
                return True
            else:
                logger.error(f"Failed to switch wallet for user {telegram_id} to {target_public_key}")
                return False
    
    async def get_wallet_info(self, telegram_id: int, public_key: str) -> Optional[Dict]:
        """
        Get detailed information about a specific wallet
        
        Args:
            telegram_id: The user's Telegram ID
            public_key: The wallet's public key
            
        Returns:
            Dictionary with wallet information or None if not found
        """
        async with self.db_pool.acquire() as conn:
            # Check if it's a legacy user's wallet
            legacy_wallet = await conn.fetchrow("""
                SELECT public_key, legacy_public_key, source_old_db 
                FROM users 
                WHERE telegram_id = $1 AND source_old_db IS NOT NULL
                AND (public_key = $2 OR legacy_public_key = $2)
            """, telegram_id, public_key)
            
            if legacy_wallet:
                if legacy_wallet['public_key'] == public_key:
                    return {
                        'public_key': public_key,
                        'type': 'current',
                        'active': True,
                        'description': 'New Turnkey Wallet',
                        'can_switch': False
                    }
                else:
                    return {
                        'public_key': public_key,
                        'type': 'legacy',
                        'active': False,
                        'description': 'Legacy Wallet (Export Only)',
                        'can_switch': False
                    }
            
            # Check if it's a new user's wallet
            turnkey_wallet = await conn.fetchrow("""
                SELECT public_key, is_active, created_at 
                FROM turnkey_wallets 
                WHERE telegram_id = $1 AND public_key = $2
            """, telegram_id, public_key)
            
            if turnkey_wallet:
                return {
                    'public_key': public_key,
                    'type': 'turnkey',
                    'active': turnkey_wallet['is_active'],
                    'description': f"Turnkey Wallet (Created: {turnkey_wallet['created_at'].strftime('%Y-%m-%d')})",
                    'can_switch': True
                }
            
            return None
