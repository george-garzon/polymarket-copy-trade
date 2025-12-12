import asyncio
import json
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from websockets import connect
from dotenv import load_dotenv
import os
from datetime import datetime, timezone

import mysql.connector
from mysql.connector import pooling

import aiohttp
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

SCRAPE = os.getenv("SCRAPE", "true").lower() == "true"
TRADE = os.getenv("TRADE", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# DRY RUN OVERRIDES
if DRY_RUN:
    SCRAPE = True
    TRADE = False

print("\n⚙️ MODE CONFIGURATION")
print(f"   SCRAPE:  {SCRAPE}")
print(f"   TRADE:   {TRADE}")
print(f"   DRY_RUN: {DRY_RUN}\n")

INFURA_WS = os.getenv("INFURA_WS")
INFURA_HTTP = os.getenv("INFURA_HTTP")
WATCH_ADDRESS = os.getenv("WATCH_WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER")
COPY_TRADE_MULTIPLIER = float(os.getenv("COPY_TRADE_MULTIPLIER", "1.0"))
ENABLE_COPY_TRADE = os.getenv("ENABLE_COPY_TRADE", "0") == "1"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "polymarket")

POLYMARKET_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLYMARKET_CTF_EXCHANGE_V2 = "0xE3f18aCc55091e2c48d883fc8C8413319d4Ab7b0"
POLYMARKET_NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
POLYMARKET_CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POLYMARKET_USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

MATCH_ORDERS_SIG = "0x2287e350"

TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH_TOPIC  = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"

# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------

POOL = pooling.MySQLConnectionPool(
    pool_name="pm_pool",
    pool_size=5,
    host=DB_HOST,
    port=DB_PORT,
    user=DB_USER,
    password=DB_PASSWORD,
    database=DB_NAME,
    autocommit=True,
)

def db_exec(query: str, params: tuple):
    cnx = POOL.get_connection()
    try:
        cur = cnx.cursor()
        cur.execute(query, params)
        cur.close()
    finally:
        cnx.close()

# ---------------------------------------------------------------------------
# Market cache (token_id -> market metadata)
# Uses CLOB Get Markets which returns condition_id + tokens[2] {token_id, outcome} :contentReference[oaicite:1]{index=1}
# ---------------------------------------------------------------------------

MARKET_CACHE = {}  # token_id -> dict(condition_id, market_slug, question, outcome)
MARKET_CACHE_LAST_REFRESH = 0
MARKET_CACHE_REFRESH_SECONDS = 600  # 10 min

def refresh_market_cache_if_needed():
    global MARKET_CACHE_LAST_REFRESH

    now = int(datetime.now(timezone.utc).timestamp())
    if now - MARKET_CACHE_LAST_REFRESH < MARKET_CACHE_REFRESH_SECONDS and MARKET_CACHE:
        return

    print("🔄 Refreshing CLOB markets cache...")

    client = ClobClient("https://clob.polymarket.com")

    next_cursor = ""
    prev_cursor = None
    seen = 0
    new_cache = {}

    MAX_PAGES = 10
    page = 0

    while True:
        page += 1
        if page > MAX_PAGES:
            print("⚠️ Market cache pagination capped")
            break

        resp = client.get_markets(next_cursor=next_cursor)
        if not isinstance(resp, dict):
            break

        data = resp.get("data") or []
        new_cursor = resp.get("next_cursor")

        for m in data:
            condition_id = m.get("condition_id")
            market_slug = m.get("market_slug")
            question = m.get("question")
            tokens = m.get("tokens") or []

            for t in tokens:
                token_id = t.get("token_id")
                outcome = t.get("outcome")

                if token_id and condition_id:
                    new_cache[str(token_id)] = {
                        "market_id": condition_id,
                        "market_slug": market_slug,
                        "market_title": question,
                        "outcome": "YES" if str(outcome).lower() == "yes" else "NO",
                    }
                    seen += 1

        prev_cursor, next_cursor = next_cursor, new_cursor
        if not next_cursor or next_cursor == prev_cursor:
            break

    MARKET_CACHE.clear()
    MARKET_CACHE.update(new_cache)
    MARKET_CACHE_LAST_REFRESH = now

    print(f"✅ Cached {seen} token→market mappings")


def market_for_token(token_id: str):
    refresh_market_cache_if_needed()
    return MARKET_CACHE.get(str(token_id))

# ---------------------------------------------------------------------------
# User profile lookup (optional)
# We'll try Gamma comments endpoint which returns a profile object with pseudonym/name if available. :contentReference[oaicite:4]{index=4}
# Cache it to avoid hammering.
# ---------------------------------------------------------------------------

USER_CACHE = {}  # address -> username or None

async def fetch_username_from_gamma(session: aiohttp.ClientSession, address: str):
    addr = address.lower()
    if addr in USER_CACHE:
        return USER_CACHE[addr]

    url = f"https://gamma-api.polymarket.com/comments/user_address/{addr}"
    try:
        async with session.get(url, timeout=10) as r:
            if r.status != 200:
                USER_CACHE[addr] = None
                return None
            data = await r.json()
            # data is a list; take first profile if present :contentReference[oaicite:5]{index=5}
            if isinstance(data, list) and data:
                prof = (data[0] or {}).get("profile") or {}
                username = prof.get("pseudonym") or prof.get("name")
                USER_CACHE[addr] = username
                return username
    except Exception:
        USER_CACHE[addr] = None
        return None

# ---------------------------------------------------------------------------
# DB upserts
# ---------------------------------------------------------------------------

def upsert_user(wallet_address: str, username: str | None):
    db_exec(
        """
        INSERT IGNORE INTO polymarket_users (wallet_address, polymarket_username, first_seen, last_seen)
        VALUES (%s, %s, NOW(), NOW())
        ON DUPLICATE KEY UPDATE
          polymarket_username = COALESCE(VALUES(polymarket_username), polymarket_username),
          last_seen = NOW()
        """,
        (wallet_address, username),
    )

def upsert_market(market_id: str, market_slug: str | None, market_title: str | None):
    db_exec(
        """
        INSERT IGNORE INTO polymarket_markets (market_id, market_slug, market_title, created_at)
        VALUES (%s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
          market_slug = COALESCE(VALUES(market_slug), market_slug),
          market_title = COALESCE(VALUES(market_title), market_title)
        """,
        (market_id, market_slug, market_title),
    )

def insert_transaction_row(
    polymarket_username: str | None,
    wallet_address: str,
    market_id: str,
    market_slug: str | None,
    market_title: str | None,
    outcome: str,
    action: str,
    quantity: float,
    price: float,
    total_cost: float,
    tx_hash: str,
    token_id: str,
    block_number: int | None,
    trade_timestamp: datetime,
):
    db_exec(
        """
        INSERT IGNORE INTO polymarket_transactions
          (polymarket_username, wallet_address, market_id, market_slug, market_title,
           outcome, action, quantity, price, total_cost,
           tx_hash, token_id, block_number, chain_id,
           trade_timestamp, scraped_at)
        VALUES
          (%s,%s,%s,%s,%s,
           %s,%s,%s,%s,%s,
           %s,%s,%s,137,
           %s,NOW())
        ON DUPLICATE KEY UPDATE
           polymarket_username = COALESCE(VALUES(polymarket_username), polymarket_username),
           market_id = VALUES(market_id),
           market_slug = COALESCE(VALUES(market_slug), market_slug),
           market_title = COALESCE(VALUES(market_title), market_title),
           outcome = VALUES(outcome),
           action = VALUES(action),
           quantity = VALUES(quantity),
           price = VALUES(price),
           total_cost = VALUES(total_cost),
           block_number = COALESCE(VALUES(block_number), block_number),
           trade_timestamp = VALUES(trade_timestamp)
        """,
        (
            polymarket_username, wallet_address, market_id, market_slug, market_title,
            outcome, action, quantity, price, total_cost,
            tx_hash, token_id, block_number, trade_timestamp
        ),
    )

def fetch_market_from_db(market_id: str):
    cnx = POOL.get_connection()
    try:
        cur = cnx.cursor(dictionary=True)
        cur.execute(
            """
            SELECT market_slug, market_title
            FROM polymarket_markets
            WHERE market_id = %s
            """,
            (market_id,)
        )
        return cur.fetchone()
    finally:
        cnx.close()


# ---------------------------------------------------------------------------
# Polymarket detection/decoding (your original code)
# ---------------------------------------------------------------------------

def is_polymarket_contract(address):
    polymarket_contracts = [
        POLYMARKET_CTF_EXCHANGE.lower(),
        POLYMARKET_CTF_EXCHANGE_V2.lower(),
        POLYMARKET_NEG_RISK_EXCHANGE.lower(),
        POLYMARKET_CONDITIONAL_TOKENS.lower(),
    ]
    return address.lower() in polymarket_contracts


def is_polymarket_transaction(tx_data, tx_receipt):
    if tx_data.get('to'):
        if is_polymarket_contract(tx_data['to']):
            input_data = tx_data.get('input', '')
            if isinstance(input_data, bytes):
                input_data = '0x' + input_data.hex()
            elif not isinstance(input_data, str):
                input_data = str(input_data)
            if input_data.startswith(MATCH_ORDERS_SIG):
                return True

    if tx_receipt and 'logs' in tx_receipt:
        for log in tx_receipt['logs']:
            if is_polymarket_contract(log.get('address', '')):
                return True

    return False


def parse_polymarket_trade(w3, tx_data, tx_receipt):
    trade_info = {
        'token_id': None,
        'side': None,
        'amount_usd': None,
        'shares': None,
    }

    watch_addr_lower = w3.to_checksum_address(WATCH_ADDRESS).lower()

    input_data = tx_data.get('input', '')
    if isinstance(input_data, bytes):
        input_data = '0x' + input_data.hex()
    elif not isinstance(input_data, str):
        input_data = str(input_data)

    if not input_data.startswith(MATCH_ORDERS_SIG):
        return trade_info

    try:
        match_orders_abi = {
            "name": "matchOrders",
            "type": "function",
            "inputs": [
                {
                    "name": "takerOrder",
                    "type": "tuple",
                    "components": [
                        {"name": "salt", "type": "uint256"},
                        {"name": "maker", "type": "address"},
                        {"name": "signer", "type": "address"},
                        {"name": "taker", "type": "address"},
                        {"name": "tokenId", "type": "uint256"},
                        {"name": "makerAmount", "type": "uint256"},
                        {"name": "takerAmount", "type": "uint256"},
                        {"name": "expiration", "type": "uint256"},
                        {"name": "nonce", "type": "uint256"},
                        {"name": "feeRateBps", "type": "uint256"},
                        {"name": "side", "type": "uint8"},
                        {"name": "signatureType", "type": "uint8"},
                        {"name": "signature", "type": "bytes"}
                    ]
                },
                {
                    "name": "makerOrders",
                    "type": "tuple[]",
                    "components": [
                        {"name": "salt", "type": "uint256"},
                        {"name": "maker", "type": "address"},
                        {"name": "signer", "type": "address"},
                        {"name": "taker", "type": "address"},
                        {"name": "tokenId", "type": "uint256"},
                        {"name": "makerAmount", "type": "uint256"},
                        {"name": "takerAmount", "type": "uint256"},
                        {"name": "expiration", "type": "uint256"},
                        {"name": "nonce", "type": "uint256"},
                        {"name": "feeRateBps", "type": "uint256"},
                        {"name": "side", "type": "uint8"},
                        {"name": "signatureType", "type": "uint8"},
                        {"name": "signature", "type": "bytes"}
                    ]
                },
                {"name": "takerFillAmount", "type": "uint256"},
                {"name": "takerReceiveAmount", "type": "uint256"},
                {"name": "makerFillAmounts", "type": "uint256[]"},
                {"name": "takerFeeAmount", "type": "uint256"},
                {"name": "makerFeeAmounts", "type": "uint256[]"}
            ]
        }

        contract = w3.eth.contract(abi=[match_orders_abi])
        decoded = contract.decode_function_input(input_data)
        params = decoded[1]

        taker_order = params['takerOrder']
        taker_fill_amount = params['takerFillAmount']
        taker_receive_amount = params['takerReceiveAmount']
        maker_fill_amounts = params.get('makerFillAmounts', [])
        maker_orders = params.get('makerOrders', [])

        if not isinstance(maker_orders, list):
            maker_orders = list(maker_orders) if maker_orders else []

        taker_maker = str(taker_order.get('maker', '')).lower()
        taker_signer = str(taker_order.get('signer', '')).lower()
        is_watched_taker = (taker_maker == watch_addr_lower or taker_signer == watch_addr_lower)

        watched_maker_idx = None
        for idx, maker_order in enumerate(maker_orders):
            maker_addr = str(maker_order.get('maker', '')).lower()
            signer_addr = str(maker_order.get('signer', '')).lower()
            if maker_addr == watch_addr_lower or signer_addr == watch_addr_lower:
                watched_maker_idx = idx
                break

        if not is_watched_taker and watched_maker_idx is None:
            return trade_info

        if is_watched_taker:
            token_id = taker_order['tokenId']
            taker_side = taker_order['side']

            trade_info['token_id'] = str(token_id)
            if taker_side == 0:
                trade_info['side'] = BUY
                trade_info['amount_usd'] = taker_fill_amount / 1e6
                trade_info['shares'] = taker_receive_amount / 1e6
            else:
                trade_info['side'] = SELL
                trade_info['amount_usd'] = taker_receive_amount / 1e6
                trade_info['shares'] = taker_fill_amount / 1e6
        else:
            watched_maker_order = maker_orders[watched_maker_idx]
            token_id = watched_maker_order['tokenId']
            maker_side = watched_maker_order['side']
            maker_fill_amount = maker_fill_amounts[watched_maker_idx] if watched_maker_idx < len(maker_fill_amounts) else 0

            trade_info['token_id'] = str(token_id)
            if maker_side == 0:
                trade_info['side'] = BUY
                maker_amount = watched_maker_order['makerAmount']
                taker_amount = watched_maker_order['takerAmount']
                if maker_amount > 0:
                    trade_info['amount_usd'] = maker_fill_amount / 1e6
                    trade_info['shares'] = (maker_fill_amount * taker_amount) / (maker_amount * 1e6)
                else:
                    trade_info['amount_usd'] = 0
                    trade_info['shares'] = 0
            else:
                trade_info['side'] = SELL
                maker_amount = watched_maker_order['makerAmount']
                taker_amount = watched_maker_order['takerAmount']
                if maker_amount > 0:
                    trade_info['shares'] = maker_fill_amount / 1e6
                    trade_info['amount_usd'] = (maker_fill_amount * taker_amount) / (maker_amount * 1e6)
                else:
                    trade_info['amount_usd'] = 0
                    trade_info['shares'] = 0

        return trade_info

    except Exception as e:
        print(f"⚠️ Error decoding matchOrders: {e}")
        import traceback
        traceback.print_exc()
        return trade_info


async def execute_copy_trade(trade_info):
    if not ENABLE_COPY_TRADE:
        return True

    try:
        client = ClobClient(
            "https://clob.polymarket.com",
            key=PRIVATE_KEY,
            chain_id=137,
            funder=FUNDER,
            signature_type=2
        )
        client.set_api_creds(client.create_or_derive_api_creds())

        amount = trade_info['amount_usd'] * COPY_TRADE_MULTIPLIER
        if amount < 1.0:
            amount = 1.0

        print(f"\n📈 Executing copy trade:")
        print(f"   Token ID: {trade_info['token_id']}")
        print(f"   Side: {trade_info['side']}")
        print(f"   Amount: ${amount:.2f}")
        print(f"   Shares: {trade_info['shares']:.2f}" if trade_info['shares'] else "   Shares: N/A")

        mo = MarketOrderArgs(
            token_id=trade_info['token_id'],
            amount=amount,
            side=trade_info['side'],
            order_type=OrderType.FOK
        )
        signed = client.create_market_order(mo)
        resp = client.post_order(signed, OrderType.FOK)
        print(resp)

    except Exception as e:
        print(f"❌ Error executing copy trade: {e}")
        import traceback
        traceback.print_exc()
        return False


def decode_single(w3, log_data):
    topics = log_data['topics']

    operator = w3.to_checksum_address("0x" + topics[1][-40:])
    from_addr = w3.to_checksum_address("0x" + topics[2][-40:])
    to_addr   = w3.to_checksum_address("0x" + topics[3][-40:])

    data_hex = log_data['data']
    data_bytes = w3.to_bytes(hexstr=data_hex)
    token_id, value = w3.codec.decode(['uint256', 'uint256'], data_bytes)

    return {
        "operator": operator,
        "from": from_addr,
        "to": to_addr,
        "ids": [token_id],
        "values": [value]
    }


def decode_batch(w3, log_data):
    topics = log_data['topics']

    operator = w3.to_checksum_address("0x" + topics[1][-40:])
    from_addr = w3.to_checksum_address("0x" + topics[2][-40:])
    to_addr   = w3.to_checksum_address("0x" + topics[3][-40:])

    data_hex = log_data['data']
    data_bytes = w3.to_bytes(hexstr=data_hex)
    ids, values = w3.codec.decode(['uint256[]', 'uint256[]'], data_bytes)

    return {
        "operator": operator,
        "from": from_addr,
        "to": to_addr,
        "ids": ids,
        "values": values
    }


async def process_transaction(web3, tx_hash, http_session: aiohttp.ClientSession):
    try:
        try:
            tx_data = web3.eth.get_transaction(tx_hash)
        except Exception:
            return

        tx_receipt = None
        for _ in range(10):
            try:
                tx_receipt = web3.eth.get_transaction_receipt(tx_hash)
                break
            except Exception:
                await asyncio.sleep(1)

        if not tx_receipt:
            print(f"⚠️ Could not get receipt for transaction {tx_hash}")
            return

        if tx_receipt.get('status') == 0:
            print(f"⚠️ Transaction {tx_hash} failed")
            return

        if not is_polymarket_transaction(tx_data, tx_receipt):
            return

        # block timestamp -> datetime
        block = web3.eth.get_block(tx_data["blockNumber"])
        trade_dt = datetime.fromtimestamp(block["timestamp"], tz=timezone.utc).replace(tzinfo=None)  # store as naive UTC

        print(f"\n🎯 Polymarket transaction detected!")
        print(f"   Tx Hash: {tx_hash}")
        print(f"   From: {tx_data.get('from')}")
        print(f"   To: {tx_data.get('to')}")

        trade_info = parse_polymarket_trade(web3, tx_data, tx_receipt)

        if trade_info['token_id'] and trade_info['side']:
            token_id = str(trade_info["token_id"])
            side = trade_info["side"]

            # Market resolve via cache
            m = MARKET_CACHE.get(token_id)
            if not m:
                m = await resolve_market_for_token(http_session, token_id)
                if m:
                    MARKET_CACHE[token_id] = m  # cache it

            market_id = (m or {}).get("market_id", "UNKNOWN")
            market_slug = (m or {}).get("market_slug")
            market_title = (m or {}).get("market_title")
            outcome = (m or {}).get("outcome", "YES")

            # Compute price + qty from amount/shares
            shares = float(trade_info["shares"] or 0.0)
            amount_usd = float(trade_info["amount_usd"] or 0.0)
            price = (amount_usd / shares) if shares > 0 else 0.0

            action = "BUY" if side == BUY else "SELL"

            # Username (best-effort)
            username = await fetch_username_from_gamma(http_session, WATCH_ADDRESS)

            # Upsert user + market
            upsert_user(WATCH_ADDRESS, username)
            if market_id != "UNKNOWN":
                upsert_market(market_id, market_slug, market_title)

            # Insert tx row
            insert_transaction_row(
                polymarket_username=username,
                wallet_address=WATCH_ADDRESS,
                market_id=market_id,
                market_slug=market_slug,
                market_title=market_title,
                outcome=outcome,
                action=action,
                quantity=shares,
                price=price,
                total_cost=amount_usd,
                tx_hash=tx_hash,
                token_id=token_id,
                block_number=int(tx_data.get("blockNumber")) if tx_data.get("blockNumber") else None,
                trade_timestamp=trade_dt,
            )

            print(f"   ✅ Saved to MySQL: market={market_slug or market_id} outcome={outcome} {action} qty={shares:.4f} price={price:.4f}")

            # Optional execution
            await execute_copy_trade(trade_info)

        else:
            print("⚠️ Could not parse trade information from transaction")

    except Exception as e:
        print(f"⚠️ Error processing transaction {tx_hash}: {e}")
        import traceback
        traceback.print_exc()


async def get_events():
    web3 = Web3(Web3.HTTPProvider(INFURA_HTTP))
    web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    watch_addr = web3.to_checksum_address(WATCH_ADDRESS)
    print(f"👀 Monitoring wallet: {watch_addr}")
    print(f"📊 Copy trade multiplier: {COPY_TRADE_MULTIPLIER}x | ENABLE_COPY_TRADE={ENABLE_COPY_TRADE}")

    # warm market cache once
    refresh_market_cache_if_needed()

    async with aiohttp.ClientSession() as http_session:
        async with connect(INFURA_WS) as ws:
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": [
                    "logs",
                    {
                        "topics": [
                            [TRANSFER_SINGLE_TOPIC, TRANSFER_BATCH_TOPIC]
                        ]
                    }
                ]
            }

            await ws.send(json.dumps(subscribe_msg))
            subscription_response = await ws.recv()
            print("✅ Connecté à Polygon via WebSocket Infura")
            print("🔌 Abonné aux events ERC-1155… en attente.")
            print(f"Réponse de souscription: {subscription_response}")

            while True:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=60)
                    response = json.loads(message)

                    if 'params' in response and 'subscription' in response['params']:
                        log_data = response['params']['result']

                        if log_data.get('topics') and len(log_data['topics']) > 0:
                            topic0 = log_data['topics'][0]

                            if topic0 == TRANSFER_SINGLE_TOPIC:
                                decoded = decode_single(web3, log_data)
                            elif topic0 == TRANSFER_BATCH_TOPIC:
                                decoded = decode_batch(web3, log_data)
                            else:
                                continue

                            if decoded["from"].lower() == watch_addr.lower() or decoded["to"].lower() == watch_addr.lower():
                                await process_transaction(web3, log_data.get('transactionHash'), http_session)

                except asyncio.TimeoutError:
                    ping_msg = {"jsonrpc": "2.0", "id": 2, "method": "eth_blockNumber", "params": []}
                    await ws.send(json.dumps(ping_msg))
                    continue
                except Exception as e:
                    print(f"⚠️ Erreur lors du traitement: {e}")
                    continue

async def resolve_market_for_token(session: aiohttp.ClientSession, token_id: str):
    # 1) Gamma has fast lookup; different deployments use different param names.
    gamma_urls = [
        ("https://gamma-api.polymarket.com/markets", {"clob_token_id": token_id}),
        ("https://gamma-api.polymarket.com/markets", {"token_id": token_id}),
    ]

    for url, params in gamma_urls:
        try:
            async with session.get(url, params=params, timeout=10) as r:
                if r.status != 200:
                    continue
                data = await r.json()

                # Gamma often returns a list of markets
                if isinstance(data, list) and data:
                    m = data[0]
                elif isinstance(data, dict) and data.get("data"):
                    m = (data["data"] or [None])[0]
                else:
                    continue

                # Try common keys
                condition_id = m.get("condition_id") or m.get("conditionId")
                market_slug  = m.get("market_slug") or m.get("slug")
                title        = m.get("question") or m.get("title")

                if condition_id:
                    # Outcome mapping: try to find which outcome matches this token_id
                    outcome = None
                    tokens = m.get("tokens") or []
                    for t in tokens:
                        if str(t.get("token_id")) == str(token_id):
                            o = (t.get("outcome") or "").strip().lower()
                            outcome = "YES" if o == "yes" else "NO"
                            break

                    return {
                        "market_id": condition_id,
                        "market_slug": market_slug,
                        "market_title": title,
                        "outcome": outcome or "YES",
                    }
        except Exception:
            pass

    return None



async def main():
    while True:
        try:
            await get_events()
        except Exception as e:
            print(f"❌ Erreur de connexion: {e}")
            print("🔄 Tentative de reconnexion dans 5 secondes...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Arrêt manuel.")
