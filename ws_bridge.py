from __future__ import annotations
import logging
import os
from quart import websocket
from livekit import rtc
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("ws-bridge")

LIVEKIT_URL = os.getenv("LIVEKIT_URL")

# Estado global del bridge
active_room: rtc.Room | None = None
active_room_name: str | None = None


async def connect_to_livekit():
    global active_room, active_room_name

    async with httpx.AsyncClient() as client:
        r = await client.get(
            "http://localhost:8000/getToken",
            params={"name": "esp32-bridge"}
        )
        data = r.json()
        token = data["token"]
        active_room_name = data["room"]

    active_room = rtc.Room()
    await active_room.connect(LIVEKIT_URL, token)
    logger.info(f"✅ Bridge conectado a sala: {active_room_name}")


async def handle_esp32_quart():
    global active_room

    logger.info("📡 ESP32 conectado")

    if active_room is None:
        await connect_to_livekit()

    try:
        while True:
            message = await websocket.receive()
    

            if message.startswith("MODE:"):
                mode = message.split(":")[1].strip()
                logger.info(f"Modo: {mode}")
                await active_room.local_participant.publish_data(message.encode())

            elif message.startswith("IMG_START:"):
                total = int(message.split(":")[1])
                logger.info(f"Imagen: {total} partes")
                await active_room.local_participant.publish_data(message.encode())

            elif message.startswith("IMG_CHUNK:"):
                await active_room.local_participant.publish_data(message.encode())

            elif message == "IMG_END":
                logger.info("✅ Imagen completa")
                await active_room.local_participant.publish_data(b"IMG_END")

            elif message.startswith("OBSTACLE:"):
                dist = message.split(":")[1]
                logger.warning(f"⚠️ Obstáculo a {dist}cm")
                await active_room.local_participant.publish_data(b"MODE:assistant")
                await active_room.local_participant.publish_data(message.encode())

            elif message.startswith("HELLO:"):
                logger.info(f"ESP32: {message}")
                await websocket.send(f"STATUS:Conectado a {active_room_name}")

            else:
                logger.debug(f"Mensaje desconocido: {message}")

    except Exception as e:
        logger.error(f"Bridge error: {e}")