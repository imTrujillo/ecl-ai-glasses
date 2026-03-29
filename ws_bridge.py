from __future__ import annotations
import asyncio
import io
import logging
import os
import edge_tts
from quart import websocket
from livekit import rtc
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("ws-bridge")

active_room: rtc.Room | None = None
active_room_name: str | None = None
esp32_websocket = None  # referencia al WS activo


async def connect_to_livekit():
    global active_room, active_room_name
    from livekit.api import AccessToken, VideoGrants

    active_room_name = os.getenv("DEFAULT_ROOM", "gafas-test")
    token = (
        AccessToken(
            os.getenv("LIVEKIT_API_KEY"),
            os.getenv("LIVEKIT_API_SECRET"),
        )
        .with_identity("esp32-bridge")
        .with_name("esp32-bridge")
        .with_grants(VideoGrants(room_join=True, room=active_room_name))
    )

    active_room = rtc.Room()

    # ── Escuchar mensajes TTS del agente ──────────────────────────────────────
    @active_room.on("data_received")
    def on_data(packet, *args, **kwargs):
        try:
            msg = packet.data.decode("utf-8")
            if msg.startswith("TTS:"):
                text = msg[4:]
                logger.info(f"TTS recibido: {text[:50]}...")
                asyncio.ensure_future(_generate_and_send_audio(text))
        except Exception as e:
            logger.error(f"Bridge data error: {e}")

    await active_room.connect(os.getenv("LIVEKIT_URL"), token.to_jwt())
    logger.info(f"✅ Bridge conectado a sala: {active_room_name}")


async def _generate_and_send_audio(text: str):
    global esp32_websocket
    if esp32_websocket is None:
        logger.warning("⚠️ Sin ESP32 conectado, descartando audio")
        return

    try:
        communicate = edge_tts.Communicate(text, "es-PY-TaniaNeural")
        audio_buffer = io.BytesIO()

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_buffer.write(chunk["data"])

        mp3_bytes = audio_buffer.getvalue()
        total = len(mp3_bytes)
        logger.info(f"🔊 Enviando audio: {total} bytes")

        await esp32_websocket.send(f"AUDIO_START:{total}")

        CHUNK = 1024
        for i in range(0, total, CHUNK):
            await esp32_websocket.send(mp3_bytes[i:i + CHUNK]) 
            await asyncio.sleep(0.01)

        await esp32_websocket.send("AUDIO_END")
        logger.info("✅ Audio enviado completo")

    except Exception as e:
        logger.error(f"Error generando audio: {e}")

async def handle_esp32_quart():
    global active_room, esp32_websocket

    logger.info("📡 ESP32 conectado")

    if active_room is None:
        await websocket.send("ERROR:Bridge no listo")
        return

    esp32_websocket = websocket._get_current_object()

    # ── Enviar bienvenida de audio inmediatamente al conectar ─────────────
    await _generate_and_send_audio("Hola, soy Navi, tu asistente de gafas inteligentes. ¿En qué puedo ayudarte?")

    try:
        while True:
            message = await websocket.receive()

            if message.startswith("MODE:"):
                mode = message.split(":")[1].strip()
                logger.info(f"Modo: {mode}")
                await active_room.local_participant.publish_data(message.encode())
                if mode != "assistant":
                    await _generate_and_send_audio(f"Modo {mode} activado.")

            elif message.startswith("IMG_START:"):
                await active_room.local_participant.publish_data(message.encode())

            elif message.startswith("IMG_CHUNK:"):
                await active_room.local_participant.publish_data(message.encode())

            elif message == "IMG_END":
                await active_room.local_participant.publish_data(b"IMG_END")

            elif message.startswith("OBSTACLE:"):
                dist = message.split(":")[1]
                await active_room.local_participant.publish_data(b"MODE:assistant")
                await active_room.local_participant.publish_data(message.encode())
                await _generate_and_send_audio(f"Atención, obstáculo detectado a {dist} centímetros.")

            elif message.startswith("HELLO:"):
                await websocket.send(f"STATUS:Conectado a {active_room_name}")

    except Exception as e:
        logger.error(f"Bridge error: {e}")
    finally:
        esp32_websocket = None