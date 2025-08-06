const AWS = require('aws-sdk');

class KMSService {
    constructor() {
        this.kms = new AWS.KMS({ region: 'us-west-1' });
        this.keyId = '27958fe3-0f3f-44d4-b21d-9d820d5ad96c';
    }

    async encryptSessionKeys(apiPublicKey, apiPrivateKey) {
        try {
            // Create a JSON object with the session keys using the field names Python expects
            const sessionData = {
                apiPublicKey: apiPublicKey,
                apiPrivateKey: apiPrivateKey
            };

            // Convert to JSON string and then to Buffer
            const plaintext = Buffer.from(JSON.stringify(sessionData), 'utf-8');

            // Encrypt using KMS with encryption context
            const params = {
                KeyId: this.keyId,
                Plaintext: plaintext,
                EncryptionContext: {
                    'Service': 'lumenbro-session-keys',
                    'Environment': process.env.NODE_ENV || 'development'
                }
            };

            const result = await this.kms.encrypt(params).promise();
            
            // Return base64 encoded ciphertext and key ID
            return {
                encryptedData: result.CiphertextBlob.toString('base64'),
                keyId: this.keyId
            };
        } catch (error) {
            console.error('KMS encryption failed:', error);
            throw error;
        }
    }

    async decryptSessionKeys(encryptedData) {
        try {
            // Decode base64 encrypted data
            const ciphertextBlob = Buffer.from(encryptedData, 'base64');

            // Decrypt using KMS with encryption context
            const params = {
                CiphertextBlob: ciphertextBlob,
                KeyId: this.keyId,
                EncryptionContext: {
                    'Service': 'lumenbro-session-keys',
                    'Environment': process.env.NODE_ENV || 'development'
                }
            };

            const result = await this.kms.decrypt(params).promise();
            
            // Parse the decrypted JSON data
            const decryptedData = JSON.parse(result.Plaintext.toString('utf-8'));
            
            return {
                apiPublicKey: decryptedData.apiPublicKey,
                apiPrivateKey: decryptedData.apiPrivateKey
            };
        } catch (error) {
            console.error('KMS decryption failed:', error);
            throw error;
        }
    }

    async testConnection() {
        try {
            const result = await this.kms.describeKey({ KeyId: this.keyId }).promise();
            console.log('KMS connection successful. Key:', result.KeyMetadata.KeyId);
            return true;
        } catch (error) {
            console.error('KMS connection failed:', error);
            return false;
        }
    }
}

module.exports = KMSService;
