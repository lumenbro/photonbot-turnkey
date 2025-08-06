# KMS Integration Summary - Node.js & Python Bot

## 🎯 **Main Goal**
Ensure consistent database and KMS usage between Node.js chart API server and Python trading bot, with shared session key encryption/decryption.

## 🔑 **KMS Configuration**

### **Shared KMS Key**
- **Key ID**: `27958fe3-0f3f-44d4-b21d-9d820d5ad96c`
- **Region**: `us-west-1`
- **Usage**: Encrypt session keys in Node.js, decrypt in Python bot

### **IAM Roles**
- **Node.js EC2** (`i-02a8485d9c74fb9fe`): `TradingBotEC2Role` ✅ **CONFIGURED**
- **Python Bot EC2** (`i-06f191c66fed97b28`): `TradingBotEC2Role` ✅ **CONFIGURED**

### **Required KMS Permissions**
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "kms:Encrypt",
                "kms:Decrypt",
                "kms:DescribeKey"
            ],
            "Resource": "arn:aws:kms:us-west-1:783906944039:key/27958fe3-0f3f-44d4-b21d-9d820d5ad96c"
        }
    ]
}
```

## 🗄️ **Shared Database Schema**

### **Users Table (Updated)**
```sql
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    public_key TEXT,
    referral_code TEXT,
    turnkey_user_id TEXT,
    turnkey_session_id TEXT,
    temp_api_public_key TEXT,        -- OLD: Direct storage
    temp_api_private_key TEXT,       -- OLD: Direct storage
    session_expiry TIMESTAMP,
    user_email TEXT,
    kms_encrypted_session_key TEXT,  -- NEW: KMS encrypted
    kms_key_id TEXT                  -- NEW: KMS key reference
);
```

### **Database Connection**
- **Host**: `lumenbro-turnkey.cz2imkksk7b4.us-west-1.rds.amazonaws.com`
- **Port**: `5434`
- **Database**: `postgres`
- **User**: `botadmin`
- **SSL**: Required (uses `global-bundle.pem`)

## 📁 **Key Files Modified**

### **Node.js Side (lumenbro.app)**
1. **`services/kmsService.js`** - KMS encryption service
2. **`routes/login.js`** - Encrypts session keys before DB storage
3. **`db.js`** - Database connection with SSL support

### **Python Side (photonbot-live)**
1. **`services/kms_service.py`** - KMS decryption service
2. **`main.py`** - Updated to use KMS decryption
3. **`test_kms.py`** - KMS connection test

## ✅ **Current Status**

### **Working Components**
- ✅ **KMS Key Access**: Both EC2s can access the same KMS key
- ✅ **Node.js Encryption**: Session keys encrypted before DB storage
- ✅ **Python KMS Service**: Can connect and describe key
- ✅ **Database Schema**: Updated to include KMS fields
- ✅ **IAM Roles**: Both EC2s have same role with KMS permissions

### **Issues to Fix**
- ❌ **Python Bot Syntax**: Indentation error in `main.py` (line 124)
- ❌ **Missing Dependencies**: `aiogram` package not installed in Python venv
- ❌ **Service Loop**: Python bot service restarting due to errors

## 🔄 **Data Flow**

### **Session Creation (Node.js)**
1. User logs in via Node.js backend
2. Turnkey creates temporary session keys
3. **Node.js encrypts** session keys using KMS
4. Encrypted data stored in `kms_encrypted_session_key` field
5. Key ID stored in `kms_key_id` field

### **Transaction Signing (Python)**
1. Python bot receives transaction request
2. Fetches `kms_encrypted_session_key` and `kms_key_id` from DB
3. **Python decrypts** session keys using KMS
4. Signs transaction with decrypted keys
5. Returns signed transaction

## 🛠️ **Next Steps**

### **Immediate Fixes (Python Bot)**
1. **Install missing packages**:
   ```bash
   source venv/bin/activate
   pip install aiogram==2.25.1
   ```

2. **Fix main.py syntax**:
   - Replace entire file with corrected version
   - Fix indentation issues
   - Test syntax: `python -m py_compile main.py`

3. **Test bot startup**:
   ```bash
   sudo systemctl restart photonbot-test.service
   journalctl -u photonbot-test.service -f
   ```

### **Testing Flow**
1. **Create session** via Node.js backend
2. **Verify encryption** in database
3. **Test transaction signing** via Python bot
4. **Verify decryption** works correctly

## 📋 **Environment Variables**

### **Node.js (.env)**
```bash
# Database
DB_HOST=lumenbro-turnkey.cz2imkksk7b4.us-west-1.rds.amazonaws.com
DB_PORT=5434
DB_NAME=postgres
DB_USER=botadmin
DB_PASSWORD=your_password
SSL_CA_PATH=./global-bundle.pem

# AWS KMS
AWS_REGION=us-west-1
KMS_KEY_ID=27958fe3-0f3f-44d4-b21d-9d820d5ad96c
```

### **Python (.env)**
```bash
# Database
DB_HOST=lumenbro-turnkey.cz2imkksk7b4.us-west-1.rds.amazonaws.com
DB_PORT=5434
DB_NAME=postgres
DB_USER=botadmin
DB_PASSWORD=your_password

# Bot
BOT_TOKEN=your_bot_token
FEE_WALLET=your_fee_wallet_address

# AWS KMS (handled by IAM role)
AWS_REGION=us-west-1
```

## 🔍 **Testing Commands**

### **KMS Connection Test**
```bash
# Python bot EC2
cd ~/photonbot-live
source venv/bin/activate
python test_kms.py
```

### **Database Connection Test**
```bash
# Test PostgreSQL connection
psql "host=lumenbro-turnkey.cz2imkksk7b4.us-west-1.rds.amazonaws.com port=5434 dbname=postgres user=botadmin sslmode=verify-full sslrootcert=global-bundle.pem"
```

### **Service Status**
```bash
# Check Python bot service
sudo systemctl status photonbot-test.service
journalctl -u photonbot-test.service -f
```

## 🎯 **Success Criteria**
- ✅ Both services can access the same KMS key
- ✅ Session keys encrypted in Node.js, decrypted in Python
- ✅ Database schema supports KMS fields
- ✅ Python bot starts without errors
- ✅ End-to-end transaction signing works

## 📞 **Key Contacts**
- **Node.js EC2**: `50.18.29.37` (photonbot-ec2)
- **Python Bot EC2**: `54.219.250.137` (lumenbro-bot)
- **Database**: RDS PostgreSQL with SSL

---

## ⚠️ **CRITICAL: Custom Stellar SDK Modifications**

### **Modified SDK Files (Must Preserve)**
When creating new virtual environments or reinstalling packages, **MUST** copy these custom-modified Stellar SDK files:

**Windows Path:**
- `C:\Python313\Lib\site-packages\stellar_sdk\call_builder\call_builder_async`
- `C:\Python313\Lib\site-packages\stellar_sdk`

**Linux/Mac Path:**
- `venv/lib/python3.x/site-packages/stellar_sdk/call_builder/call_builder_async`
- `venv/lib/python3.x/site-packages/stellar_sdk`

### **Backup/Restore Process**
```bash
# Before creating new venv, backup modified files
cp -r /path/to/old/venv/lib/python3.x/site-packages/stellar_sdk /backup/

# After creating new venv and installing stellar-sdk==12.2.0
cp -r /backup/stellar_sdk /new/venv/lib/python3.x/site-packages/
```

### **Identification Method**
- **Modified files** have different timestamps than the rest of the library
- **Original files** all have same creation/modified timestamps
- **Custom edits** from Grok 4 integration must be preserved

---

**Last Updated**: August 6, 2025
**Status**: KMS integration working, Python bot needs syntax fixes
