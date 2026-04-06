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
_reconnecting = False

_img_buffer       = bytearray()
_img_total        = 0
_img_mode         = "describe"
_collecting_image = False


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

    room = rtc.Room()

    @room.on("data_received")
    def on_data(packet, *args, **kwargs):
        try:
            msg = packet.data.decode("utf-8")
            if msg.startswith("TTS:"):
                text = msg[4:]
                asyncio.ensure_future(_generate_and_send_audio(text))
        except Exception as e:
            logger.error(f"Bridge data error: {e}")

    @room.on("disconnected")
    def on_disconnected(*args, **kwargs):
        logger.warning("⚠️ LiveKit desconectado")
        asyncio.ensure_future(_reconnect_livekit())

    await room.connect(os.getenv("LIVEKIT_URL"), token.to_jwt())
    active_room = room
    logger.info(f"✅ Bridge conectado a sala: {active_room_name}")


async def _reconnect_livekit():
    global active_room, _reconnecting

    if _reconnecting:
        return

    _reconnecting = True
    active_room = None
    delay = 5

    while True:
        await asyncio.sleep(delay)
        try:
            await connect_to_livekit()
            logger.info("✅ Bridge reconectado")
            _reconnecting = False
            return
        except Exception as e:
            delay = min(delay * 2, 60)
            logger.error(f"❌ Reconexión fallida (próximo en {delay}s): {e}")


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
            logger.info(f"🎤 TTS: '{text[:60]}'")
            communicate = edge_tts.Communicate(text, "es-PY-TaniaNeural")
            mp3_buffer = io.BytesIO()

            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    mp3_buffer.write(chunk["data"])

            mp3_bytes = mp3_buffer.getvalue()
            if not mp3_bytes:
                logger.error("❌ edge-tts vacío")
                return

            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-i", "pipe:0",
                "-ar", "16000",
                "-ac", "1",
                "-acodec", "pcm_s16le",
                "-f", "wav",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            wav_bytes, _ = await proc.communicate(input=mp3_bytes)

            if not wav_bytes:
                logger.error("❌ ffmpeg vacío")
                return

            logger.info(f"🔊 WAV: {len(wav_bytes)} bytes")

            if esp32_websocket is None:
                return

            # En _generate_and_send_audio, reemplaza el loop de chunks:

            await current_socket.send(f"AUDIO_START:{len(wav_bytes)}")
            await asyncio.sleep(0.15)

            CHUNK = 4096
            offset = 0
            while offset < len(wav_bytes):
                chunk = wav_bytes[offset:offset + CHUNK]
                await current_socket.send_bytes(chunk)  # ✅ send_bytes en lugar de send
                offset += len(chunk)
                await asyncio.sleep(0.02)

            await current_socket.send("AUDIO_END")
            logger.info("✅ Audio enviado")

        except Exception as e:
            logger.error(f"❌ Error audio: {e}")
            esp32_websocket = None


async def handle_esp32_quart():
    global esp32_websocket, _img_buffer, _img_total, _img_mode, _collecting_image

    logger.info("📡 ESP32 conectando...")

    if active_room is None:
        try:
            await connect_to_livekit()
        except Exception as e:
            logger.error(f"❌ LiveKit: {e}")
            await websocket.send("ERROR:Bridge no listo")
            return

    for i in range(15):
        if active_room is not None:
            break
        logger.warning(f"⏳ Esperando LiveKit {i+1}/15")
        await asyncio.sleep(1)

    if active_room is None:
        await websocket.send("ERROR:Bridge no listo")
        return

    # ✅ Solo reemplazar si el socket anterior está realmente muerto
    if esp32_websocket is not None:
        logger.warning("⚠️ Reemplazando ESP32 anterior")
        old = esp32_websocket
        esp32_websocket = None
        try:
            await old.close()
        except Exception:
            pass
        await asyncio.sleep(0.5)

    esp32_websocket = websocket._get_current_object()
    logger.info("✅ ESP32 registrado")

    # Publicar en LiveKit que el bridge está activo
    if active_room:
        try:
            await active_room.local_participant.publish_data(
                b"BRIDGE:connected", reliable=True
            )
        except Exception:
            pass

    await _generate_and_send_audio("Hola, soy Navi, tu asistente de gafas inteligentes.")

    try:
        while True:
            message = await websocket.receive()

            if isinstance(message, bytes):
                if _collecting_image:
                    _img_buffer.extend(message)
                continue

            logger.info(f"[WS] 📨 {message[:80]}")

            if message.startswith("HELLO:"):
                await websocket.send(f"STATUS:Conectado a {active_room_name}")

            elif message.startswith("MODE:"):
                mode = message.split(":")[1].strip()
                logger.info(f"[MODE] → {mode}")
                if active_room:
                    await active_room.local_participant.publish_data(
                        message.encode(), reliable=True
                    )
                if mode != "assistant":
                    await _generate_and_send_audio(f"Modo {mode} activado.")

            elif message.startswith("IMG_START:"):
                parts = message.split(":")
                _img_mode  = parts[1] if len(parts) > 2 else "describe"
                _img_total = int(parts[-1])
                _img_buffer = bytearray()
                _collecting_image = True
                logger.info(f"[IMG] 📥 modo={_img_mode} total={_img_total}")

            elif message == "IMG_END":
                _collecting_image = False
                received = len(_img_buffer)
                logger.info(f"[IMG] ✅ {received}/{_img_total} bytes")

                if active_room and received > 0:
                    img_b64 = base64.b64encode(_img_buffer).decode("utf-8")
                    await active_room.local_participant.publish_data(
                        f"IMG_START:{_img_mode}:0".encode(), reliable=True
                    )
                    await active_room.local_participant.publish_data(
                        f"IMG_CHUNK:0:{img_b64}".encode(), reliable=True
                    )
                    await active_room.local_participant.publish_data(
                        b"IMG_END", reliable=True
                    )
                    logger.info("[IMG] ✅ Enviado al agente")

            elif message.startswith("OBSTACLE:"):
                dist = message.split(":")[1]
                if active_room:
                    await active_room.local_participant.publish_data(
                        message.encode(), reliable=True
                    )
                await _generate_and_send_audio(
                    f"Atención, obstáculo a {dist} centímetros."
                )

            else:
                logger.info(f"[WS] ❓ {message[:50]}")

    except Exception as e:
        logger.error(f"❌ Bridge error: {e}")
    finally:
        esp32_websocket = None
        _collecting_image = False
        logger.info("📡 ESP32 desconectado")