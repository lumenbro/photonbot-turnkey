from stellar_sdk import Keypair, TransactionEnvelope
import os
import logging

logger = logging.getLogger(__name__)

class LocalSigner:
    def __init__(self, app_context):
        self.app_context = app_context
        test_signer_secret = os.getenv("TEST_SIGNER_SECRET")
        if not test_signer_secret:
            raise ValueError("TEST_SIGNER_SECRET not found in .env for TEST_MODE")
        try:
            self.keypair = Keypair.from_secret(test_signer_secret)
            logger.info(f"LocalSigner initialized with public key: {self.keypair.public_key}")
            logger.debug(f"LocalSigner: Using network passphrase: {self.app_context.network_passphrase}")
        except Exception as e:
            raise ValueError(f"Invalid TEST_SIGNER_SECRET: {str(e)}")

    async def sign_transaction(self, telegram_id, transaction_xdr):
        logger.info(f"LocalSigner: Signing transaction for telegram_id {telegram_id}")
        logger.debug(f"LocalSigner: Transaction XDR length: {len(transaction_xdr)}")
        logger.debug(f"LocalSigner: Network passphrase: {self.app_context.network_passphrase}")
        
        try:
            env = TransactionEnvelope.from_xdr(transaction_xdr, self.app_context.network_passphrase)
            logger.debug(f"LocalSigner: Transaction envelope parsed successfully")
            logger.debug(f"LocalSigner: Transaction source: {env.transaction.source}")
            logger.debug(f"LocalSigner: Transaction operations count: {len(env.transaction.operations)}")
            
            env.sign(self.keypair)
            logger.debug(f"LocalSigner: Transaction signed successfully")
            
            signed_xdr = env.to_xdr()
            logger.debug(f"LocalSigner: Signed XDR length: {len(signed_xdr)}")
            return signed_xdr
        except Exception as e:
            logger.error(f"LocalSigner: Error signing transaction: {str(e)}")
            raise
