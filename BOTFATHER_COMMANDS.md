# BotFather Menu Commands Setup

## 🎯 Enhanced Buy/Sell Commands

Your bot now supports both chat commands and BotFather menu commands for enhanced trading functionality.

## 📱 BotFather Menu Setup

### **Step 1: Contact BotFather**
1. Open Telegram and search for `@BotFather`
2. Start a chat with BotFather

### **Step 2: Set Menu Commands**
Send this command to BotFather:

```
/setcommands
```

### **Step 3: Select Your Bot**
Choose your bot from the list (e.g., `@stellartradingbottest_bot`)

### **Step 4: Enter Menu Commands**
Copy and paste this exact text:

```
start - Start the bot and show main menu
buy - Buy assets with enhanced menu (XLM amounts, market data)
sell - Sell assets with percentage options and market data
balance - Check your wallet balance and asset values
register - Register a new Turnkey wallet
login - Login to your existing wallet
help - Show help and FAQ information
```

### **Step 5: Confirm**
BotFather will confirm the menu has been set.

## 🎮 Available Commands

### **Core Trading Commands:**
- **`/buy`** - Enhanced buy flow with XLM amounts and market data
- **`/sell`** - Enhanced sell flow with asset selection and percentages
- **`/balance`** - Check wallet balance and asset values

### **Wallet Management:**
- **`/register`** - Register new Turnkey wallet
- **`/login`** - Login to existing wallet
- **`/start`** - Show main menu

### **Help & Support:**
- **`/help`** - Show help and FAQ

## 🚀 Enhanced Features

### **Buy Command (`/buy`):**
1. Enter asset code:issuer
2. Enhanced buy menu with:
   - Market data (price, market cap, volume)
   - Quick buy buttons (25, 50, 100, 200 XLM)
   - Custom amount with persistence
   - Real-time price calculations

### **Sell Command (`/sell`):**
1. Asset selection menu (shows your assets)
2. Enhanced sell menu with:
   - Market data and asset info
   - Percentage-based selling (10%, 25%, 50%, 100%)
   - Custom percentage input
   - Real-time balance updates

## 💡 User Experience

### **Before (Old Flow):**
- Click menu button → Enter code:issuer → Enter amount → Trade

### **After (Enhanced Flow):**
- Type `/buy` or `/sell` → Enhanced menus → Trade

### **Benefits:**
- ✅ **Faster access** - Direct commands
- ✅ **Better UX** - Rich menus with market data
- ✅ **Professional feel** - Standard bot conventions
- ✅ **Discoverable** - Commands show in bot menu

## 🔧 Technical Implementation

The commands integrate seamlessly with existing functionality:
- Uses same enhanced buy/sell flows
- Same error handling and cleanup
- Same market data integration
- Same percentage-based selling

## 📋 Testing Checklist

- [ ] `/buy` command works
- [ ] `/sell` command works
- [ ] Menu commands appear in bot menu
- [ ] Enhanced flows function correctly
- [ ] Error handling works
- [ ] Message cleanup works

## 🎉 Result

Users can now:
1. **Type `/buy`** → Get enhanced buy menu
2. **Type `/sell`** → Get enhanced sell menu
3. **Use menu buttons** → Same enhanced experience
4. **Access commands** → Via bot menu or direct typing

This makes your bot much more professional and user-friendly! 🚀
