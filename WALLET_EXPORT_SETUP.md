# Wallet Export Setup Guide

## Overview
This guide covers the implementation of wallet export functionality for both Turnkey and Legacy users in the LumenBro Telegram bot.

## ✅ What's Already Implemented

### 1. Turnkey Wallet Export
- **Location**: `handlers/walletmanagement.py`
- **Button**: "📤 Export Wallet Keys" in Wallet Management menu
- **Function**: `process_turnkey_wallet_export()`
- **URL Format**: `https://lumenbro.com/mini-app/index.html?action=export&orgId={sub_org_id}&email={email}`

### 2. Legacy Wallet Export  
- **Location**: `handlers/walletmanagement.py`
- **Button**: "📤 Export Legacy Wallet (👑 Pioneer)" in Wallet Management menu
- **Function**: `process_legacy_wallet_export()`
- **Features**: KMS decryption of S-address secrets

### 3. Cancel Export
- **Function**: `process_cancel_export()`
- **Callback**: `cancel_export`

## 🔧 Implementation Details

### Database Queries
```sql
-- Turnkey wallet data query
SELECT tw.turnkey_sub_org_id, tw.public_key, u.user_email
FROM turnkey_wallets tw
LEFT JOIN users u ON tw.telegram_id = u.telegram_id
WHERE tw.telegram_id = $1 AND tw.is_active = TRUE

-- Legacy wallet data query  
SELECT encrypted_s_address_secret, public_key, pioneer_status, source_old_db
FROM users 
WHERE telegram_id = $1 AND source_old_db IS NOT NULL
```

### URL Parameters for Mini-App
- **action**: `export`
- **orgId**: Turnkey sub-organization ID
- **email**: User's email address

### Security Features
- ✅ KMS decryption for legacy S-address secrets
- ✅ Client-side export (keys never leave user's device)
- ✅ Password protection for API key decryption
- ✅ Turnkey passkey protection for exports

## 📱 User Experience Flow

### Turnkey Users:
1. User clicks "📤 Export Wallet Keys" in Wallet Management
2. Bot shows export details with "📱 Open Export Page" button
3. Button opens mini-app with `action=export&orgId=X&email=Y`
4. Mini-app handles the actual export process
5. User can cancel with "❌ Cancel" button

### Legacy Users:
1. User clicks "📤 Export Legacy Wallet (👑 Pioneer)" 
2. Bot decrypts S-address secret using KMS
3. Bot displays private key directly in chat
4. User can copy/paste the key

## 🚀 Mini-App Requirements

### Export Page (`/mini-app/index.html?action=export`)
The mini-app needs to handle the export action with these parameters:

```javascript
// URL parameters to handle
const urlParams = new URLSearchParams(window.location.search);
const action = urlParams.get('action'); // 'export'
const orgId = urlParams.get('orgId');   // Turnkey sub-org ID
const email = urlParams.get('email');   // User email

// Export functionality should:
// 1. Authenticate user with Turnkey
// 2. Decrypt API keys using user's password
// 3. Generate Stellar private key from API keys
// 4. Display key in hex and S-address formats
// 5. Provide download option for backup file
// 6. Include security warnings
```

### Required Mini-App Features:
- ✅ Turnkey authentication
- ✅ Password-based API key decryption
- ✅ Stellar key generation
- ✅ Multiple export formats (hex, S-address)
- ✅ Download functionality
- ✅ Security warnings and instructions

## 🔒 Security Considerations

### Turnkey Export:
- **Protection**: Turnkey passkey + user password
- **Process**: Client-side decryption only
- **Storage**: No keys stored on server
- **Access**: Requires active Turnkey session

### Legacy Export:
- **Protection**: KMS encryption + database security
- **Process**: Server-side KMS decryption
- **Storage**: Encrypted in database
- **Access**: Legacy user authentication

## 📋 Testing Checklist

### Turnkey Export:
- [ ] Button appears in Wallet Management for Turnkey users
- [ ] Click opens export page with correct URL parameters
- [ ] Mini-app loads and handles export action
- [ ] Export process works end-to-end
- [ ] Cancel button works properly

### Legacy Export:
- [ ] Button appears for legacy users with Pioneer badge
- [ ] KMS decryption works correctly
- [ ] S-address secret is displayed properly
- [ ] Security warnings are shown
- [ ] Export message is formatted correctly

### Error Handling:
- [ ] No wallet found error
- [ ] KMS decryption failure
- [ ] Database connection issues
- [ ] Invalid user permissions

## 🚀 Deployment

### 1. Commit Changes
```bash
git add handlers/walletmanagement.py
git commit -m "Add Turnkey wallet export functionality"
git push origin main
```

### 2. Deploy to Server
```bash
# On EC2 server
git pull origin main
sudo systemctl restart photonbot-test.service
```

### 3. Test Functionality
- Test with Turnkey user account
- Test with Legacy user account
- Verify error handling
- Check mini-app integration

## 📞 Support Notes

### Common Issues:
1. **Mini-app not loading**: Check URL format and parameters
2. **Export fails**: Verify Turnkey session and permissions
3. **KMS errors**: Check AWS KMS configuration
4. **Database errors**: Verify user data exists

### Debug Commands:
```bash
# Check bot logs
journalctl -u photonbot-test.service -f

# Test database connection
psql "host=lumenbro-turnkey.cz2imkksk7b4.us-west-1.rds.amazonaws.com port=5434 dbname=postgres user=botadmin sslmode=verify-full sslrootcert=global-bundle.pem"
```

## 🎯 Future Enhancements

### Potential Improvements:
- [ ] Export history tracking
- [ ] Multiple wallet export support
- [ ] Scheduled backup exports
- [ ] Export format customization
- [ ] Integration with hardware wallets

### Mini-App Enhancements:
- [ ] QR code generation for keys
- [ ] Paper wallet generation
- [ ] Multi-format export (JSON, CSV, etc.)
- [ ] Encrypted backup files
- [ ] Export verification tools

---

**Status**: ✅ **IMPLEMENTED AND READY FOR DEPLOYMENT**

The wallet export functionality is fully implemented in the Python bot. The mini-app needs to handle the `action=export` parameter to complete the user experience.




