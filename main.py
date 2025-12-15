import asyncio
import json
import os
import re
import time
import httpx

from datetime import datetime
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from websockets import connect
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL


# -------------------------------------------------
# ENV
# -------------------------------------------------

load_dotenv()

INFURA_WS = os.getenv("INFURA_WS")
INFURA_HTTP = os.getenv("INFURA_HTTP")
WATCH_ADDRESS = os.getenv("WATCH_WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER")
COPY_TRADE_MULTIPLIER = float(os.getenv("COPY_TRADE_MULTIPLIER", "1.0"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


# -------------------------------------------------
# CONSTANTS
# -------------------------------------------------

POLYMARKET_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLYMARKET_CTF_EXCHANGE_V2 = "0xE3f18aCc55091e2c48d883fc8C8413319d4Ab7b0"
POLYMARKET_NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
POLYMARKET_CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

MATCH_ORDERS_SIG = "0x2287e350"

TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH_TOPIC = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"


# -------------------------------------------------
# BTC 15M STATE
# -------------------------------------------------

CURRENT_BTC_15M = {
    "slug": None,
    "token_ids": set(),
}


# -------------------------------------------------
# MARKET DISCOVERY (ROBUST)
# -------------------------------------------------

def find_current_btc_15min_market() -> str:
    page_url = "https://polymarket.com/crypto/15M"
    resp = httpx.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()

    matches = re.findall(r"btc-updown-15m-(\d+)", resp.text)
    if not matches:
        raise RuntimeError("No BTC 15m markets found")

    now = int(time.time())
    valid = [int(ts) for ts in matches if int(ts) <= now + 60]
    return f"btc-updown-15m-{max(valid)}"


def load_current_btc_15m_tokens():
    """
    Polls CLOB until the current BTC 15m market becomes available.
    """
    slug = find_current_btc_15min_market()
    client = ClobClient("https://clob.polymarket.com")

    print(f"🔍 Waiting for CLOB market: {slug}")

    while True:
        markets = client.get_markets()
        if isinstance(markets, dict):
            markets = markets.get("markets", [])

        for m in markets:
            if not isinstance(m, dict):
                continue

            if m.get("slug") != slug:
                continue

            token_ids = {
                str(o["token_id"])
                for o in m.get("outcomes", [])
                if "token_id" in o
            }

            if token_ids:
                CURRENT_BTC_15M["slug"] = slug
                CURRENT_BTC_15M["token_ids"] = token_ids

                print(f"✅ BTC 15m market live in CLOB")
                print(f"   Slug  : {slug}")
                print(f"   Tokens: {token_ids}")
                return

        print("⏳ Market not in CLOB yet — retrying in 5s...")
        time.sleep(5)


# -------------------------------------------------
# HELPERS
# -------------------------------------------------

def is_polymarket_contract(address):
    return address.lower() in {
        POLYMARKET_CTF_EXCHANGE.lower(),
        POLYMARKET_CTF_EXCHANGE_V2.lower(),
        POLYMARKET_NEG_RISK_EXCHANGE.lower(),
        POLYMARKET_CONDITIONAL_TOKENS.lower(),
    }


def is_polymarket_transaction(tx_data, tx_receipt):
    if tx_data.get("to") and is_polymarket_contract(tx_data["to"]):
        input_data = tx_data.get("input", "")
        if isinstance(input_data, bytes):
            input_data = "0x" + input_data.hex()
        if input_data.startswith(MATCH_ORDERS_SIG):
            return True

    for log in tx_receipt.get("logs", []):
        if is_polymarket_contract(log.get("address", "")):
            return True

    return False


# -------------------------------------------------
# TRADE PARSER (UNCHANGED)
# -------------------------------------------------

def parse_polymarket_trade(w3, tx_data, tx_receipt):
    trade = {"token_id": None, "side": None, "amount_usd": None, "shares": None}

    input_data = tx_data.get("input", "")
    if isinstance(input_data, bytes):
        input_data = "0x" + input_data.hex()

    if not input_data.startswith(MATCH_ORDERS_SIG):
        return trade

    try:
        contract = w3.eth.contract(abi=[{"name": "matchOrders", "type": "function"}])
        _, params = contract.decode_function_input(input_data)

        taker = params["takerOrder"]

        trade["token_id"] = str(taker["tokenId"])
        trade["side"] = BUY if taker["side"] == 0 else SELL
        trade["amount_usd"] = params["takerFillAmount"] / 1e6
        trade["shares"] = params["takerReceiveAmount"] / 1e6

        return trade
    except Exception:
        return trade


# -------------------------------------------------
# COPY TRADE
# -------------------------------------------------

async def execute_copy_trade(trade):
    amount = max(trade["amount_usd"] * COPY_TRADE_MULTIPLIER, 1.0)

    print("\n📈 COPY TRADE")
    print(f"   Market : {CURRENT_BTC_15M['slug']}")
    print(f"   Token  : {trade['token_id']}")
    print(f"   Side   : {trade['side']}")
    print(f"   Amount : ${amount:.2f}")

    if DRY_RUN:
        print("🧪 DRY RUN — no order sent")
        return

    client = ClobClient(
        "https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=137,
        funder=FUNDER,
        signature_type=2,
    )

    client.set_api_creds(client.create_or_derive_api_creds())

    mo = MarketOrderArgs(
        token_id=trade["token_id"],
        amount=amount,
        side=trade["side"],
        order_type=OrderType.FOK,
    )

    signed = client.create_market_order(mo)
    print(client.post_order(signed, OrderType.FOK))


# -------------------------------------------------
# TX HANDLER
# -------------------------------------------------

async def process_transaction(web3, tx_hash):
    try:
        tx_data = web3.eth.get_transaction(tx_hash)
        tx_receipt = web3.eth.get_transaction_receipt(tx_hash)

        if not is_polymarket_transaction(tx_data, tx_receipt):
            return

        trade = parse_polymarket_trade(web3, tx_data, tx_receipt)

        if trade["token_id"] not in CURRENT_BTC_15M["token_ids"]:
            return

        await execute_copy_trade(trade)
    except Exception:
        pass


# -------------------------------------------------
# EVENT LOOP
# -------------------------------------------------

async def get_events():
    web3 = Web3(Web3.HTTPProvider(INFURA_HTTP))
    web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    load_current_btc_15m_tokens()

    async with connect(INFURA_WS) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": ["logs", {"topics": [[TRANSFER_SINGLE_TOPIC, TRANSFER_BATCH_TOPIC]]}],
        }))

        while True:
            msg = json.loads(await ws.recv())
            log = msg.get("params", {}).get("result")
            if log:
                await process_transaction(web3, log["transactionHash"])


async def main():
    while True:
        try:
            await get_events()
        except Exception as e:
            print(f"❌ Reconnecting: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
