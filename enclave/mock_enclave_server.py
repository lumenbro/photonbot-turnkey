import base64
import json
import logging
import socket
import os
from stellar_sdk import Keypair, Network, TransactionEnvelope
from cryptography.fernet import Fernet

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# VSOCK configuration
VSOCK_PORT = 5000

def decrypt_data_key(ciphertext_blob, aws_credentials):
    try:
        logger.debug(f"Requesting KMS decryption from parent for ciphertext: {ciphertext_blob[:20]}...")
        # Instead of calling KMS directly, send the ciphertext to the parent over VSOCK
        # We'll use a separate VSOCK port (e.g., 8001) for KMS requests
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        parent_cid = 3  # Parent instance CID
        kms_port = 8001  # Port for KMS requests
        sock.connect((parent_cid, kms_port))

        # Send the ciphertext to the parent
        request = {
            "action": "kms_decrypt",
            "ciphertext": ciphertext_blob,
            "aws_credentials": aws_credentials
        }
        request_data = json.dumps(request).encode('utf-8')
        length_prefix = len(request_data).to_bytes(4, byteorder='big')
        sock.send(length_prefix + request_data)

        # Receive the response
        length_prefix = sock.recv(4)
        length = int.from_bytes(length_prefix, byteorder='big')
        response_data = sock.recv(length).decode('utf-8')
        response = json.loads(response_data)

        if "error" in response:
            raise ValueError(response["error"])
        
        plaintext = base64.b64decode(response["plaintext"])
        logger.debug(f"Received plaintext from parent: {plaintext[:10]}...")
        return {"Plaintext": plaintext}
    except Exception as e:
        logger.error(f"Error requesting KMS decryption from parent: {str(e)}")
        raise
    finally:
        sock.close()

def generate_keypair(request):
    try:
        telegram_id = request["telegram_id"]
        encrypted_data_key = request["encrypted_data_key"]
        data_key = base64.b64decode(request["data_key"])
        
        mnemonic_phrase = Keypair.generate_mnemonic_phrase(strength=256)
        kp = Keypair.from_mnemonic_phrase(mnemonic_phrase, index=0)
        
        cipher = Fernet(base64.urlsafe_b64encode(data_key))
        encrypted_secret = cipher.encrypt(kp.secret.encode()).hex()
        
        return {
            "telegram_id": str(telegram_id),
            "public_key": kp.public_key,
            "encrypted_secret": encrypted_secret,
            "encrypted_data_key": encrypted_data_key,
            "recovery_secret": mnemonic_phrase
        }
    except Exception as e:
        logger.error(f"Error in generate_keypair: {str(e)}")
        return {"error": str(e)}

def sign_transaction(request, aws_credentials=None):
    try:
        encrypted_secret = bytes.fromhex(request["encrypted_secret"])
        encrypted_data_key = request["encrypted_data_key"]
        transaction_xdr = request["transaction_xdr"]
        public_key = request["public_key"]
        logger.debug(f"Signing transaction for public_key: {public_key}")
        
        kms_response = decrypt_data_key(encrypted_data_key, aws_credentials)
        data_key = base64.urlsafe_b64encode(kms_response["Plaintext"])
        cipher = Fernet(data_key)
        secret = cipher.decrypt(encrypted_secret).decode()
        
        kp = Keypair.from_secret(secret)
        if kp.public_key != public_key:
            logger.error("Public key mismatch")
            return {"error": "Public key mismatch"}
        
        # For local mock, default to PUBLIC unless app provides otherwise
        tx_envelope = TransactionEnvelope.from_xdr(transaction_xdr, network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE)
        tx_envelope.sign(kp)
        signed_xdr = tx_envelope.to_xdr()
        logger.debug(f"Signed XDR length: {len(signed_xdr)}")
        return {"signed_transaction": signed_xdr}
    except Exception as e:
        logger.error(f"Error in sign_transaction: {str(e)}")
        return {"error": str(e)}

def handle_connection(conn):
    try:
        length_prefix = conn.recv(4)
        if len(length_prefix) != 4:
            raise ValueError("Failed to read length prefix")
        length = int.from_bytes(length_prefix, byteorder='big')
        logger.debug(f"Expecting message of length: {length}")
        
        data = conn.recv(length).decode('utf-8')
        request = json.loads(data)
        logger.debug(f"Received data: {request}")
        
        action = request.get("action")
        if action == "generate":
            response = generate_keypair(request)
        elif action == "sign":
            aws_credentials = request.get("aws_credentials", {})
            response = sign_transaction(request, aws_credentials)
        else:
            response = {"error": "Unknown action"}
        
        response_data = json.dumps(response).encode('utf-8')
        length_prefix = len(response_data).to_bytes(4, byteorder='big')
        logger.debug(f"Response length: {len(response_data)} bytes")
        conn.send(length_prefix + response_data)
    except Exception as e:
        logger.error(f"Connection error: {str(e)}")
    finally:
        conn.close()

def main():
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.bind((socket.VMADDR_CID_ANY, VSOCK_PORT))
    sock.listen(1)
    logger.info(f"Listening on VSOCK port {VSOCK_PORT}")
    
    while True:
        conn, addr = sock.accept()
        logger.debug(f"Accepted connection from {addr}")
        handle_connection(conn)

if __name__ == "__main__":
    main()
