import asyncio
import json
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from websockets import connect
from dotenv import load_dotenv
import os
from datetime import datetime
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

INFURA_WS = os.getenv("INFURA_WS")
INFURA_HTTP = os.getenv("INFURA_HTTP")
WATCH_ADDRESS = os.getenv("WATCH_WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
COPY_TRADE_MULTIPLIER = float(os.getenv("COPY_TRADE_MULTIPLIER", "1.0"))

POLYMARKET_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLYMARKET_CTF_EXCHANGE_V2 = "0xE3f18aCc55091e2c48d883fc8C8413319d4Ab7b0"
POLYMARKET_NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
POLYMARKET_CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POLYMARKET_USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

MATCH_ORDERS_SIG = "0x2287e350"

TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH_TOPIC  = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"


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
    try:
        client = ClobClient(
            "https://clob.polymarket.com",
            key=PRIVATE_KEY,
            chain_id=137,
            signature_type=SIGNATURE_TYPE,
            funder=FUNDER
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        
        amount = trade_info['amount_usd'] * COPY_TRADE_MULTIPLIER
        
        if amount < 0.01:
            print(f"⚠️ Copy trade amount too small: ${amount:.2f}")
            return False
        
        print(f"\n📈 Executing copy trade:")
        print(f"   Token ID: {trade_info['token_id']}")
        print(f"   Side: {trade_info['side']}")
        print(f"   Amount: ${amount:.2f}")
        print(f"   Shares: {trade_info['shares']:.2f}" if trade_info['shares'] else "   Shares: N/A")

        mo = MarketOrderArgs(token_id=trade_info['token_id'], amount=1.00, side=trade_info['side'], order_type=OrderType.FOK)
        signed = client.create_market_order(mo)
        resp = client.post_order(signed, OrderType.FOK)
        print(resp)

        
    except Exception as e:
        print(f"❌ Error executing copy trade: {e}")
        import traceback
        traceback.print_exc()
        return False

async def send_alert(event_type, log_data, decoded):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n🚨 NOUVEL ERC-1155 DÉTECTÉ !")
    print(f"Timestamp: {timestamp}")
    print(f"Type: {event_type}")
    print(f"Contrat: {log_data['address']}")
    print(f"Tx Hash: {log_data.get('transactionHash', 'N/A')}")
    print(f"From: {decoded['from']}")
    print(f"To: {decoded['to']}")
    print(f"Token IDs: {decoded['ids']}")
    print(f"Values: {decoded['values']}")
    print("-" * 40)


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


async def process_transaction(web3, tx_hash):
    try:
        try:
            tx_data = web3.eth.get_transaction(tx_hash)
        except Exception as e:
            return
        
        if tx_data.get('to') and not is_polymarket_contract(tx_data['to']):
            pass
        
        tx_receipt = None
        for attempt in range(10):
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
        
        print(f"\n🎯 Polymarket transaction detected!")
        print(f"   Tx Hash: {tx_hash}")
        print(f"   From: {tx_data.get('from')}")
        print(f"   To: {tx_data.get('to')}")
        
        trade_info = parse_polymarket_trade(web3, tx_data, tx_receipt)
        
        if trade_info['token_id'] and trade_info['side']:
            print(f"   Token ID: {trade_info['token_id']}")
            print(f"   Side: {trade_info['side']}")
            print(f"   Amount: ${trade_info['amount_usd']:.2f}" if trade_info['amount_usd'] else "   Amount: N/A")
            print(f"   Shares: {trade_info['shares']:.2f}" if trade_info['shares'] else "   Shares: N/A")
            
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
    print(f"📊 Copy trade multiplier: {COPY_TRADE_MULTIPLIER}x")
    
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
            await process_transaction(web3, "0x8fe6af21be3a382cc348e62f6125fadc4359487166c8f8ef01bff491c0b020e0")
            await asyncio.sleep(100)
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=60)
                response = json.loads(message)
                
                if 'params' in response and 'subscription' in response['params']:
                    log_data = response['params']['result']
                    
                    if log_data.get('topics') and len(log_data['topics']) > 0:
                        topic0 = log_data['topics'][0]
                        
                        if topic0 == TRANSFER_SINGLE_TOPIC:
                            decoded = decode_single(web3, log_data)
                            event_type = "TransferSingle"
                        elif topic0 == TRANSFER_BATCH_TOPIC:
                            decoded = decode_batch(web3, log_data)
                            event_type = "TransferBatch"
                        else:
                            continue
                        
                        if decoded["from"].lower() == watch_addr.lower() or decoded["to"].lower() == watch_addr.lower():
                            await send_alert(event_type, log_data, decoded)
                            await process_transaction(web3, log_data.get('transactionHash'))
                            
            except asyncio.TimeoutError:
                ping_msg = {"jsonrpc": "2.0", "id": 2, "method": "eth_blockNumber", "params": []}
                await ws.send(json.dumps(ping_msg))
                continue
            except Exception as e:
                print(f"⚠️ Erreur lors du traitement: {e}")
                continue


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
