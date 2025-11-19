import asyncio
import json
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from websockets import connect
from dotenv import load_dotenv
import os
from datetime import datetime

load_dotenv()

INFURA_WS = os.getenv("INFURA_WS")
INFURA_HTTP = os.getenv("INFURA_HTTP")
WATCH_ADDRESS = os.getenv("WATCH_WALLET_ADDRESS")

TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH_TOPIC  = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"


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


async def get_events():
    web3 = Web3(Web3.HTTPProvider(INFURA_HTTP))
    web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    
    watch_addr = web3.to_checksum_address(WATCH_ADDRESS)
    print(f"👀 Monitoring du wallet : {watch_addr}")
    
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
                            event_type = "TransferSingle"
                        elif topic0 == TRANSFER_BATCH_TOPIC:
                            decoded = decode_batch(web3, log_data)
                            event_type = "TransferBatch"
                        else:
                            continue
                        
                        if decoded["from"].lower() == watch_addr.lower() or decoded["to"].lower() == watch_addr.lower():
                            await send_alert(event_type, log_data, decoded)
                            
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
