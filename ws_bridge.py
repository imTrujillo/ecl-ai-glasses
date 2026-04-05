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
    global active_room
    await asyncio.sleep(5)
    try:
        active_room = None
        await connect_to_livekit()
        logger.info("✅ Bridge reconectado")
    except Exception as e:
        logger.error(f"❌ Reconexión fallida: {e}")
        asyncio.ensure_future(_reconnect_livekit())

async def _generate_and_send_audio(text: str):
    global esp32_websocket

    current_socket = esp32_websocket
    if current_socket is None:
        return

    if _audio_lock.locked():
        logger.warning("⚠️ Audio en proceso, descartando")
        return

    async with _audio_lock:
        try:
            # 1. Generar MP3 con edge-tts
            communicate = edge_tts.Communicate(text, "es-PY-TaniaNeural")
            mp3_buffer = io.BytesIO()

            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    mp3_buffer.write(chunk["data"])

            mp3_bytes = mp3_buffer.getvalue()
            if not mp3_bytes:
                return

            # 2. ✅ Convertir MP3 → WAV con ffmpeg directo (sin pydub)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-i", "pipe:0",           # entrada desde stdin
                "-ar", "16000",           # 16kHz
                "-ac", "1",               # mono
                "-acodec", "pcm_s16le",   # 16-bit PCM
                "-f", "wav",
                "pipe:1",                 # salida a stdout
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            wav_bytes, _ = await proc.communicate(input=mp3_bytes)

            if not wav_bytes:
                logger.error("❌ ffmpeg no generó WAV")
                return

            logger.info(f"🔊 Enviando WAV: {len(wav_bytes)} bytes")

            if esp32_websocket is None:
                return

            await current_socket.send(f"AUDIO_START:{len(wav_bytes)}")
            await asyncio.sleep(0.05)
            await current_socket.send(wav_bytes)
            await current_socket.send("AUDIO_END")
            logger.info("✅ Audio WAV enviado")

        except Exception as e:
            logger.error(f"Error enviando audio: {e}")
            esp32_websocket = None

async def handle_esp32_quart():
    global esp32_websocket

    logger.info("📡 ESP32 conectado")

    # ✅ Conectar a LiveKit solo cuando hay un ESP32 real
    if active_room is None:
        logger.info("Conectando bridge a LiveKit...")
        try:
            await connect_to_livekit()
        except Exception as e:
            logger.error(f"❌ No se pudo conectar: {e}")
            await websocket.send("ERROR:Bridge no listo")
            return

    # ✅ Si LiveKit no está listo, esperar hasta 10s
    for _ in range(10):
        if active_room is not None:
            break
        logger.warning("⏳ Esperando LiveKit...")
        await asyncio.sleep(1)

    if active_room is None:
        await websocket.send("ERROR:Bridge no listo")
        return

    if esp32_websocket is not None:
        old = esp32_websocket
        esp32_websocket = None
        try:
            await old.close()
        except Exception:
            pass
        await asyncio.sleep(0.3)

    esp32_websocket = websocket._get_current_object()
    logger.info("✅ ESP32 registrado")

    await _generate_and_send_audio("Hola, soy Navi, tu asistente de gafas inteligentes.")

    try:
        while True:
            message = await websocket.receive()

            if isinstance(message, bytes):
                continue  # ignorar binario entrante

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
                if active_room:
                    await active_room.local_participant.publish_data(message.encode())
                await _generate_and_send_audio(f"Atención, obstáculo a {dist} centímetros.")

            elif message.startswith("HELLO:"):
                await websocket.send(f"STATUS:Conectado a {active_room_name}")

    except Exception as e:
        logger.error(f"Bridge error: {e}")
    finally:
        esp32_websocket = None
        logger.info("📡 ESP32 desconectado")