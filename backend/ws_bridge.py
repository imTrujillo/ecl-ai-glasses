"""
ws_bridge.py — Puente WebSocket entre ESP32 y LiveKit
======================================================
Corre junto a server.py y agent.py.
El ESP32 se conecta aquí vía WiFi, y este bridge
retransmite los datos al agente de LiveKit.

Correr con:
    python ws_bridge.py
"""
import asyncio
import base64
import logging
import os

import websockets
from livekit import rtc
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ws-bridge")

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
SERVER_URL  = "http://localhost:8000"

# Sala y token se obtienen dinámicamente
active_room: rtc.Room | None = None
active_token: str | None = None
active_room_name: str | None = None


async def get_token(room_name: str = None) -> tuple[str, str]:
    import httpx
    async with httpx.AsyncClient() as client:
        params = {"name": "esp32-bridge"}
        if room_name:
            params["room"] = room_name
        r = await client.get(f"{SERVER_URL}/getToken", params=params)
        data = r.json()
        return data["token"], data["room"]


async def connect_to_livekit(room_name: str = None):
    global active_room, active_token, active_room_name

    token, room = await get_token(room_name)
    active_token     = token
    active_room_name = room

    active_room = rtc.Room()
    await active_room.connect(LIVEKIT_URL, token)
    logger.info(f"Bridge conectado a LiveKit sala: {room}")
    return active_room


async def handle_esp32(websocket):
    global active_room

    logger.info(f"ESP32 conectado desde {websocket.remote_address}")

    # Conectar a LiveKit si no está conectado
    if active_room is None or not active_room.connection_state:
        await connect_to_livekit()

    image_chunks = {"parts": [], "total": 0}

    try:
        async for message in websocket:
            if isinstance(message, str):

                # ── Modo ────────────────────────────────────────────────────
                if message.startswith("MODE:"):
                    mode = message.split(":")[1]
                    logger.info(f"ESP32 → Modo: {mode}")
                    await active_room.local_participant.publish_data(
                        message.encode()
                    )

                # ── Audio del micrófono ──────────────────────────────────────
                elif message.startswith("AUDIO:"):
                    audio_b64 = message[6:]
                    await active_room.local_participant.publish_data(
                        message.encode()
                    )

                # ── Fin de audio ─────────────────────────────────────────────
                elif message == "AUDIO_END":
                    await active_room.local_participant.publish_data(
                        b"AUDIO_END"
                    )

                # ── Imagen en chunks ─────────────────────────────────────────
                elif message.startswith("IMG_START:"):
                    total = int(message.split(":")[1])
                    image_chunks["parts"] = []
                    image_chunks["total"] = total
                    logger.info(f"Recibiendo imagen en {total} partes...")
                    await active_room.local_participant.publish_data(
                        message.encode()
                    )

                elif message.startswith("IMG_CHUNK:"):
                    image_chunks["parts"].append(message)
                    await active_room.local_participant.publish_data(
                        message.encode()
                    )

                elif message == "IMG_END":
                    logger.info("Imagen completa — enviando a LiveKit")
                    await active_room.local_participant.publish_data(
                        b"IMG_END"
                    )

                # ── Obstáculo detectado ──────────────────────────────────────
                elif message.startswith("OBSTACLE:"):
                    dist = message.split(":")[1]
                    logger.warning(f"⚠️ Obstáculo a {dist}cm")
                    # Enviar alerta al agente
                    alert = f"MODE:assistant"
                    await active_room.local_participant.publish_data(
                        alert.encode()
                    )
                    # También enviar el mensaje de obstáculo directo
                    await active_room.local_participant.publish_data(
                        f"OBSTACLE:{dist}".encode()
                    )

                elif message.startswith("HELLO:"):
                    logger.info(f"ESP32 identificado: {message}")
                    await websocket.send(f"STATUS:Conectado a sala {active_room_name}")

    except websockets.exceptions.ConnectionClosed:
        logger.info("ESP32 desconectado")
    except Exception as e:
        logger.error(f"Error bridge: {e}")


async def main():
    logger.info("Iniciando WebSocket bridge en puerto 8765...")
    logger.info("El ESP32 debe conectarse a ws://TU_IP:8765/ws")

    async with websockets.serve(handle_esp32, "0.0.0.0", 8765):
        await asyncio.Future()  # correr para siempre


if __name__ == "__main__":
    asyncio.run(main())
