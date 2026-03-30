from __future__ import annotations
import asyncio
import base64
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
esp32_websocket = None
_audio_lock = asyncio.Lock()


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

    @active_room.on("disconnected")
    def on_disconnected(*args, **kwargs):
        logger.warning("⚠️ LiveKit desconectado — reconectando en 5s...")
        asyncio.ensure_future(_reconnect_livekit())

    await active_room.connect(os.getenv("LIVEKIT_URL"), token.to_jwt())
    logger.info(f"✅ Bridge conectado a sala: {active_room_name}")


async def _reconnect_livekit():
    await asyncio.sleep(5)
    try:
        await connect_to_livekit()
        logger.info("✅ Bridge reconectado a LiveKit")
    except Exception as e:
        logger.error(f"❌ Reconexión fallida: {e}")
        asyncio.ensure_future(_reconnect_livekit())


async def _generate_and_send_audio(text: str):
    global esp32_websocket

    # Capturar referencia local — evita race condition
    current_socket = esp32_websocket
    if current_socket is None:
        logger.warning("⚠️ Sin ESP32, descartando audio")
        return

    if _audio_lock.locked():
        logger.warning("⚠️ Audio en proceso, descartando nuevo")
        return

    async with _audio_lock:
        try:
            # Generar MP3 con edge-tts
            communicate = edge_tts.Communicate(text, "es-PY-TaniaNeural")
            audio_buffer = io.BytesIO()

            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_buffer.write(chunk["data"])

            mp3_bytes = audio_buffer.getvalue()
            if not mp3_bytes:
                logger.warning("⚠️ Audio vacío")
                return

            # ── Convertir a base64 ──────────────────────────
            b64_audio = base64.b64encode(mp3_bytes).decode("utf-8")
            total_b64  = len(b64_audio)
            CHUNK_SIZE = 10000  # mismo tamaño que el ESP32 usa para imágenes

            total_chunks = (total_b64 + CHUNK_SIZE - 1) // CHUNK_SIZE
            logger.info(f"🔊 Enviando audio: {len(mp3_bytes)} bytes → {total_b64} b64 → {total_chunks} chunks")

            # Verificar que el socket sigue vivo antes de enviar
            if esp32_websocket is None:
                logger.warning("⚠️ ESP32 desconectado antes de enviar")
                return

            await current_socket.send(f"AUDIO_START:{total_chunks}")
            await asyncio.sleep(0.1)

            for i in range(total_chunks):
                # Verificar mid-stream
                if esp32_websocket is None:
                    logger.warning("⚠️ ESP32 desconectado mid-stream")
                    return

                chunk = b64_audio[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
                await current_socket.send(f"AUDIO_CHUNK:{i}:{chunk}")
                await asyncio.sleep(0.05)

            await current_socket.send("AUDIO_END")
            logger.info("✅ Audio enviado completo")

        except Exception as e:
            logger.error(f"Error enviando audio: {e}")
            esp32_websocket = None


async def handle_esp32_quart():
    global esp32_websocket

    logger.info("📡 ESP32 conectado")

    if active_room is None:
        await websocket.send("ERROR:Bridge no listo")
        return

    # Cerrar conexión anterior limpiamente
    if esp32_websocket is not None:
        logger.warning("⚠️ Reemplazando conexión ESP32 anterior")
        old_socket = esp32_websocket
        esp32_websocket = None
        try:
            await old_socket.close()
        except Exception:
            pass
        await asyncio.sleep(0.5)

    esp32_websocket = websocket._get_current_object()
    logger.info("✅ ESP32 registrado")

    await _generate_and_send_audio(
        "Hola, soy Navi, tu asistente de gafas inteligentes."
    )

    try:
        while True:
            message = await websocket.receive()

            if isinstance(message, bytes):
                continue

            logger.debug(f"ESP32 → {message[:60]}")

            if message.startswith("MODE:"):
                mode = message.split(":")[1].strip()
                logger.info(f"Modo: {mode}")
                if active_room:
                    await active_room.local_participant.publish_data(message.encode())
                if mode != "assistant":
                    await _generate_and_send_audio(f"Modo {mode} activado.")

            elif message.startswith("IMG_START:"):
                if active_room:
                    await active_room.local_participant.publish_data(message.encode())

            elif message.startswith("IMG_CHUNK:"):
                if active_room:
                    await active_room.local_participant.publish_data(message.encode())

            elif message == "IMG_END":
                if active_room:
                    await active_room.local_participant.publish_data(b"IMG_END")

            elif message.startswith("OBSTACLE:"):
                dist = message.split(":")[1]
                logger.warning(f"⚠️ Obstáculo: {dist}cm")
                if active_room:
                    await active_room.local_participant.publish_data(message.encode())
                await _generate_and_send_audio(
                    f"Atención, obstáculo a {dist} centímetros."
                )

            elif message.startswith("HELLO:"):
                logger.info(f"HELLO recibido: {message}")
                await websocket.send(f"STATUS:Conectado a {active_room_name}")

    except Exception as e:
        logger.error(f"Bridge error: {e}")
    finally:
        esp32_websocket = None
        logger.info("📡 ESP32 desconectado")