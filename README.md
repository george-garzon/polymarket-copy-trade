# Polymarket Copy Trade

A real-time copy trading bot for Polymarket that monitors a target wallet and automatically replicates trades with configurable position sizing. The bot executes copy trades in less than 1 second after detecting the original trade.

## Features

- **Real-time Monitoring**: Uses WebSocket connections to monitor blockchain transactions instantly
- **Fast Execution**: Executes copy trades in less than 1 second after detecting the original trade
- **Configurable Position Sizing**: Uses `COPY_TRADE_MULTIPLIER` to scale trade amounts (e.g., 0.5x for half size, 2.0x for double size)
- **Automatic Trade Detection**: Monitors `WATCH_WALLET_ADDRESS` for Polymarket trades and automatically copies them
- **Buy/Sell Support**: Copies both buy and sell orders from the watched wallet

## How It Works

1. **Monitoring**: The bot subscribes to ERC-1155 transfer events on Polygon via WebSocket
2. **Detection**: When a transaction involving the `WATCH_WALLET_ADDRESS` is detected, it analyzes the transaction to extract trade details
3. **Parsing**: Extracts token ID, side (buy/sell), and trade amount from the Polymarket transaction
4. **Execution**: Calculates the copy trade amount using `COPY_TRADE_MULTIPLIER` and places a market order via the Polymarket CLOB API
5. **Speed**: The entire process from detection to order placement happens in less than 1 second

## Setup

### Prerequisites

- Python 3.7+
- Infura account (for WebSocket and HTTP RPC endpoints)

### Installation

```bash
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file in the project root with the following variables:

```env
# Infura endpoints (required)
INFURA_WS=wss://polygon-mainnet.infura.io/ws/v3/YOUR_PROJECT_ID
INFURA_HTTP=https://polygon-mainnet.infura.io/v3/YOUR_PROJECT_ID

# Wallet to monitor (required)
WATCH_WALLET_ADDRESS=0x...

# Your trading wallet credentials (required)
PRIVATE_KEY=0x...
FUNDER=0x...

# Copy trade multiplier (optional, default: 1.0)
# Use 0.5 for half size, 2.0 for double size, etc.
COPY_TRADE_MULTIPLIER=1.0
```

### Running the Bot

```bash
python main.py
```

## Configuration

### COPY_TRADE_MULTIPLIER

The `COPY_TRADE_MULTIPLIER` environment variable controls how much capital to use relative to the original trade:

- `1.0` - Same size as the original trade
- `0.5` - Half the size of the original trade
- `2.0` - Double the size of the original trade
- `0.1` - 10% of the original trade size

**Note**: The minimum trade amount is $1.00 USD. If the calculated amount is less than $1.00, it will be rounded up to $1.00.
