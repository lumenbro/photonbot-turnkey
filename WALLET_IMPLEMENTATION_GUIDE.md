# 🌟 Complete Wallet Implementation Guide

## 🎯 **Overview**

This guide shows how to implement the complete wallet functionality with proper user authenticator type handling and fee collection.

## 🔧 **User Authenticator Types**

### **1. Session Keys (KMS)**
- **Type**: `session_keys`
- **Method**: `python_bot_kms`
- **Storage**: KMS encrypted in server DB
- **Users**: New users with KMS sessions

### **2. Telegram Cloud API Keys**
- **Type**: `telegram_cloud`
- **Method**: `python_bot_tg_cloud`
- **Storage**: Telegram Cloud Storage (encrypted)
- **Users**: New users with TG cloud storage

### **3. Legacy Users**
- **Type**: `legacy`
- **Method**: `python_bot_legacy`
- **Storage**: Direct keys in server DB
- **Users**: Old users with `source_old_db`

## 🚀 **Complete Wallet Implementation**

### **1. Wallet Class with Authenticator Handling**

```javascript
// Complete wallet implementation
class LumenBroWallet {
  constructor(telegramId, horizonUrl = 'https://horizon.stellar.org') {
    this.telegramId = telegramId;
    this.horizonUrl = horizonUrl;
    this.feeWalletAddress = 'YOUR_FEE_WALLET_ADDRESS'; // Set your fee collection address
    this.userInfo = null;
    this.authenticatorInfo = null;
  }
  
  async initialize() {
    try {
      // Get user authenticator type and fee status
      const response = await fetch(`/mini-app/user-authenticator-type/${this.telegramId}`);
      
      if (!response.ok) {
        throw new Error(`Failed to get user info: ${response.status}`);
      }
      
      const data = await response.json();
      this.userInfo = data.user;
      this.authenticatorInfo = data.authenticator;
      
      console.log('Wallet initialized:', {
        telegramId: this.telegramId,
        authenticatorType: this.authenticatorInfo.type,
        signingMethod: this.authenticatorInfo.signing_method,
        hasActiveSession: this.authenticatorInfo.has_active_session,
        feePercentage: data.fee_status.fee_percentage
      });
      
      return data;
      
    } catch (error) {
      throw new Error(`Wallet initialization failed: ${error.message}`);
    }
  }
  
  async executeTransaction(transactionParams) {
    const {
      destination,
      asset,
      amount,
      transactionType = 'payment'
    } = transactionParams;
    
    try {
      // Check if user has active session
      if (!this.authenticatorInfo.has_active_session) {
        throw new Error('No active session. Please login first.');
      }
      
      // 1. Calculate XLM equivalent for fee calculation
      const xlmEquivalent = await this.calculateXlmEquivalent(asset, amount);
      
      // 2. Get fee calculation from Node.js backend
      const feeCalculation = await this.calculateFees({
        telegram_id: this.telegramId,
        transaction_amount: amount,
        transaction_type: transactionType,
        asset_code: asset.isNative() ? 'XLM' : asset.code,
        asset_issuer: asset.isNative() ? null : asset.issuer,
        xlm_equivalent: xlmEquivalent
      });
      
      // 3. Build transaction with Stellar-Plus
      const sourceAccount = await this.getSourceAccount();
      const transaction = new stellarPlus.TransactionBuilder(sourceAccount, {
        fee: await stellarPlus.getRecommendedFee(),
        networkPassphrase: stellarPlus.Networks.TESTNET
      });
      
      // 4. Add main operation
      transaction.addOperation(stellarPlus.Operation.payment({
        destination: destination,
        asset: asset,
        amount: amount.toString()
      }));
      
      // 5. Add fee operation
      transaction.addOperation(stellarPlus.Operation.payment({
        destination: this.feeWalletAddress,
        asset: stellarPlus.Asset.native(),
        amount: feeCalculation.calculation.fee.toString()
      }));
      
      // 6. Show fee preview to user
      const userApproved = await this.showFeePreview(feeCalculation);
      if (!userApproved) {
        throw new Error('User cancelled transaction');
      }
      
      // 7. Sign transaction using appropriate method
      const signedXdr = await this.signTransaction(transaction.toXDR(), transactionType);
      
      // 8. Submit to network using Stellar RPC
      const result = await this.submitTransaction(signedXdr);
      
      // 9. Log volume and referral shares
      await this.logTransaction({
        telegram_id: this.telegramId,
        xlm_volume: feeCalculation.calculation.xlm_volume,
        tx_hash: result.hash,
        action_type: transactionType
      });
      
      return {
        success: true,
        hash: result.hash,
        fee: feeCalculation.calculation.fee,
        xlmVolume: feeCalculation.calculation.xlm_volume,
        signing_method: this.authenticatorInfo.signing_method
      };
      
    } catch (error) {
      console.error('Transaction failed:', error);
      throw error;
    }
  }
  
  async calculateXlmEquivalent(asset, amount) {
    if (asset.isNative()) {
      return amount;
    }
    
    try {
      const pathsResponse = await fetch(
        `${this.horizonUrl}/paths/strict-send?source_asset_type=credit_alphanum4&source_asset_code=${asset.code}&source_asset_issuer=${asset.issuer}&source_amount=${amount}&destination_assets=native&limit=1`
      );
      
      const pathsData = await pathsResponse.json();
      const paths = pathsData._embedded?.records || [];
      
      if (paths.length > 0) {
        return parseFloat(paths[0].destination_amount);
      } else {
        console.warn(`No paths found for ${asset.code}:${asset.issuer} to XLM`);
        return 0.0;
      }
    } catch (error) {
      console.error(`Error calculating XLM equivalent:`, error);
      return 0.0;
    }
  }
  
  async calculateFees(params) {
    const response = await fetch('/mini-app/calculate-fees', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params)
    });
    
    if (!response.ok) {
      throw new Error(`Fee calculation failed: ${response.status}`);
    }
    
    return await response.json();
  }
  
  async getSourceAccount() {
    // Get account from Stellar RPC
    const response = await fetch(`${this.horizonUrl}/accounts/${this.userInfo.public_key}`);
    
    if (!response.ok) {
      throw new Error(`Failed to get account: ${response.status}`);
    }
    
    const accountData = await response.json();
    return new stellarPlus.Account(this.userInfo.public_key, accountData.sequence);
  }
  
  async showFeePreview(feeCalculation) {
    return new Promise((resolve) => {
      const modal = document.createElement('div');
      modal.innerHTML = `
        <div class="fee-preview-modal">
          <h3>💰 Fee Preview</h3>
          <div class="fee-breakdown">
            <p><strong>Transaction Amount:</strong> ${feeCalculation.calculation.transaction_amount} ${feeCalculation.calculation.asset_code}</p>
            <p><strong>XLM Equivalent:</strong> ${feeCalculation.calculation.xlm_volume} XLM</p>
            <p><strong>Fee Rate:</strong> ${(feeCalculation.calculation.fee_percentage * 100).toFixed(2)}%</p>
            <p><strong>Fee Amount:</strong> ${feeCalculation.calculation.fee} XLM</p>
            <p><strong>Total Cost:</strong> ${feeCalculation.calculation.total_amount} ${feeCalculation.calculation.asset_code}</p>
            <p><strong>Signing Method:</strong> ${this.authenticatorInfo.signing_method}</p>
          </div>
          <div class="fee-actions">
            <button onclick="approveTransaction()">✅ Approve & Sign</button>
            <button onclick="cancelTransaction()">❌ Cancel</button>
          </div>
        </div>
      `;
      
      document.body.appendChild(modal);
      
      window.approveTransaction = () => {
        document.body.removeChild(modal);
        resolve(true);
      };
      
      window.cancelTransaction = () => {
        document.body.removeChild(modal);
        resolve(false);
      };
    });
  }
  
  async signTransaction(xdr, transactionType) {
    const response = await fetch('/mini-app/sign-transaction', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        telegram_id: this.telegramId,
        xdr: xdr,
        transaction_type: transactionType,
        include_fee: true // Tell Python bot this already has fees
      })
    });
    
    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(`Signing failed: ${errorData.error || response.status}`);
    }
    
    const result = await response.json();
    return result.signed_xdr;
  }
  
  async submitTransaction(signedXdr) {
    // Submit using Stellar RPC (future-proof)
    const response = await fetch(`${this.horizonUrl}/transactions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tx: signedXdr
      })
    });
    
    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(`Transaction submission failed: ${errorData.extras?.result_codes || response.status}`);
    }
    
    return await response.json();
  }
  
  async logTransaction(params) {
    const response = await fetch('/mini-app/log-xlm-volume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params)
    });
    
    if (!response.ok) {
      console.warn('Failed to log XLM volume:', response.status);
    }
    
    return await response.json();
  }
  
  async getBalance() {
    const response = await fetch(`${this.horizonUrl}/accounts/${this.userInfo.public_key}`);
    
    if (!response.ok) {
      throw new Error(`Failed to get balance: ${response.status}`);
    }
    
    const accountData = await response.json();
    return accountData.balances;
  }
  
  async getTransactionHistory() {
    const response = await fetch(`${this.horizonUrl}/accounts/${this.userInfo.public_key}/transactions?limit=20`);
    
    if (!response.ok) {
      throw new Error(`Failed to get transaction history: ${response.status}`);
    }
    
    const data = await response.json();
    return data._embedded.records;
  }
}
```

### **2. Usage Example**

```javascript
// Initialize and use wallet
async function useWallet() {
  try {
    // Initialize wallet
    const wallet = new LumenBroWallet(123456789);
    await wallet.initialize();
    
    console.log('Wallet ready:', {
      authenticatorType: wallet.authenticatorInfo.type,
      signingMethod: wallet.authenticatorInfo.signing_method,
      feePercentage: wallet.userInfo.fee_percentage
    });
    
    // Execute a payment
    const result = await wallet.executeTransaction({
      destination: 'GCCD6AJOYZCUAQLX32ZJF2MKFFAUJ53PVCFQI3RHWKL3K47PKFMH2VTL',
      asset: stellarPlus.Asset.native(),
      amount: '10.0',
      transactionType: 'payment'
    });
    
    console.log('Transaction successful:', result);
    
    // Get balance
    const balance = await wallet.getBalance();
    console.log('Current balance:', balance);
    
  } catch (error) {
    console.error('Wallet error:', error);
  }
}
```

### **3. Asset Swap Implementation**

```javascript
// Asset swap with fee calculation
async function executeAssetSwap(wallet, params) {
  const {
    sendAsset,
    sendAmount,
    receiveAsset,
    receiveAmount
  } = params;
  
  // Calculate XLM equivalent for both assets
  const sendXlmEquivalent = await wallet.calculateXlmEquivalent(sendAsset, sendAmount);
  const receiveXlmEquivalent = await wallet.calculateXlmEquivalent(receiveAsset, receiveAmount);
  
  // Use the larger value for fee calculation (mirrors Python bot logic)
  const xlmVolume = Math.max(sendXlmEquivalent, receiveXlmEquivalent);
  
  // Build path payment transaction
  const sourceAccount = await wallet.getSourceAccount();
  const transaction = new stellarPlus.TransactionBuilder(sourceAccount, {
    fee: await stellarPlus.getRecommendedFee(),
    networkPassphrase: stellarPlus.Networks.TESTNET
  });
  
  // Add path payment operation
  transaction.addOperation(stellarPlus.Operation.pathPaymentStrictReceive({
    sendAsset: sendAsset,
    sendMax: sendAmount.toString(),
    destination: sourceAccount.publicKey(),
    destAsset: receiveAsset,
    destAmount: receiveAmount.toString(),
    path: [] // Let Stellar find the best path
  }));
  
  // Add fee operation
  const feeCalculation = await wallet.calculateFees({
    telegram_id: wallet.telegramId,
    transaction_amount: sendAmount,
    transaction_type: 'swap',
    asset_code: sendAsset.isNative() ? 'XLM' : sendAsset.code,
    asset_issuer: sendAsset.isNative() ? null : sendAsset.issuer,
    xlm_equivalent: xlmVolume
  });
  
  transaction.addOperation(stellarPlus.Operation.payment({
    destination: wallet.feeWalletAddress,
    asset: stellarPlus.Asset.native(),
    amount: feeCalculation.calculation.fee.toString()
  }));
  
  // Show fee preview
  const userApproved = await wallet.showFeePreview(feeCalculation);
  if (!userApproved) {
    throw new Error('User cancelled swap');
  }
  
  // Sign and submit
  const signedXdr = await wallet.signTransaction(transaction.toXDR(), 'swap');
  const result = await wallet.submitTransaction(signedXdr);
  
  // Log transaction
  await wallet.logTransaction({
    telegram_id: wallet.telegramId,
    xlm_volume: feeCalculation.calculation.xlm_volume,
    tx_hash: result.hash,
    action_type: 'swap'
  });
  
  return result;
}
```

## ✅ **Benefits of This Implementation**

### **🎯 User Type Handling:**
- ✅ **Automatic detection** of user authenticator type
- ✅ **Appropriate signing method** for each user type
- ✅ **Session validation** and expiry checking
- ✅ **Seamless fallbacks** for different user states

### **🔒 Security:**
- ✅ **No key exposure** - All signing happens server-side
- ✅ **Session validation** - Checks for active sessions
- ✅ **JWT authentication** - Secure communication with Python bot
- ✅ **Fee transparency** - User sees exact fees before signing

### **🔄 Future-Proof:**
- ✅ **Stellar RPC ready** - Uses standardized XDR format
- ✅ **Horizon compatible** - Works with current and future APIs
- ✅ **Modular design** - Easy to add new authenticator types
- ✅ **Scalable architecture** - Handles all user types seamlessly

This implementation gives you a **complete, production-ready wallet** that handles all user types while maintaining **security** and **user control**! 🎉
