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

# ── Logging detallado ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
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

# ── Throttle para obstáculos — no spamear audio ───────────────────────────────
_last_obstacle_audio = 0.0
OBSTACLE_AUDIO_COOLDOWN = 4.0  # segundos entre alertas de voz

async def _ensure_agent_dispatched():
    """Crea el dispatch del agente si no existe."""
    from livekit.api import LiveKitAPI
    from livekit import api
    
    logger.info("🤖 Verificando dispatch del agente...")
    lk = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    try:
        existing = await lk.agent_dispatch.list_dispatch(room_name=active_room_name)
        dispatches = getattr(existing, "agent_dispatches", [])
        if dispatches:
            logger.info(f"⚡ Agente ya despachado ({len(dispatches)} dispatch activo)")
            return
        
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="smart-glasses",
                room=active_room_name
            )
        )
        logger.info("✅ Dispatch del agente creado")
    except Exception as e:
        logger.error(f"❌ Error creando dispatch: {e}")
    finally:
        await lk.aclose()

async def connect_to_livekit():
    global active_room, active_room_name
    from livekit.api import AccessToken, VideoGrants

    active_room_name = os.getenv("DEFAULT_ROOM", "gafas-test")
    logger.info(f"🔌 Conectando a LiveKit — sala: {active_room_name}")

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
            logger.info(f"📨 LiveKit→Bridge: {msg[:80]}")
            if msg.startswith("TTS:"):
                text = msg[4:]
                logger.info(f"🔊 TTS recibido del agente: '{text[:60]}'")
                asyncio.ensure_future(_generate_and_send_audio(text))
        except Exception as e:
            logger.error(f"❌ Bridge data error: {e}")

    @room.on("participant_connected")
    def on_participant(*args, **kwargs):
        logger.info(f"👤 Participante conectado a LiveKit")

    @room.on("participant_disconnected")
    def on_participant_left(*args, **kwargs):
        logger.warning(f"👤 Participante desconectado de LiveKit")

    @room.on("disconnected")
    def on_disconnected(*args, **kwargs):
        logger.warning("⚠️ LiveKit desconectado — iniciando reconexión")
        asyncio.ensure_future(_reconnect_livekit())

    await room.connect(os.getenv("LIVEKIT_URL"), token.to_jwt())
    active_room = room
    logger.info(f"✅ Bridge conectado a LiveKit — sala: {active_room_name}")


async def _reconnect_livekit():
    global active_room, _reconnecting

    if _reconnecting:
        return

    _reconnecting = True
    active_room = None
    delay = 5

    while True:
        await asyncio.sleep(delay)
        logger.info(f"🔄 Intentando reconectar LiveKit...")
        try:
            await connect_to_livekit()
            logger.info("✅ Bridge reconectado a LiveKit")
            _reconnecting = False
            return
        except Exception as e:
            delay = min(delay * 2, 60)
            logger.error(f"❌ Reconexión fallida (próximo en {delay}s): {e}")


async def _generate_and_send_audio(text: str):
    global esp32_websocket

    current_socket = esp32_websocket
    if current_socket is None:
        logger.warning("⚠️ TTS ignorado — ESP32 no conectado")
        return

    if _audio_lock.locked():
        logger.warning(f"⚠️ Audio ocupado — descartando: '{text[:40]}'")
        return

    async with _audio_lock:
        try:
            logger.info(f"🎤 Generando TTS: '{text[:60]}'")
            communicate = edge_tts.Communicate(text, "es-PY-TaniaNeural")
            mp3_buffer = io.BytesIO()

            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    mp3_buffer.write(chunk["data"])

            mp3_bytes = mp3_buffer.getvalue()
            if not mp3_bytes:
                logger.error("❌ edge_tts no generó audio")
                return

            logger.info(f"🎵 MP3 generado: {len(mp3_bytes)} bytes — convirtiendo a RAW 8kHz...")

            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-i", "pipe:0",
                "-ar", "8000",
                "-ac", "1",
                "-f", "u8",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            raw_bytes, _ = await proc.communicate(input=mp3_bytes)

            if not raw_bytes:
                logger.error("❌ ffmpeg no produjo salida")
                return

            logger.info(f"🔊 RAW listo: {len(raw_bytes)} bytes — enviando al ESP32...")

            # Re-verificar socket después de la conversión
            if esp32_websocket is None:
                logger.warning("⚠️ ESP32 se desconectó durante generación de audio")
                return

            # ✅ Enviar todo de una vez — sin chunks ni sleeps intermedios
            await current_socket.send(f"AUDIO_START:{len(raw_bytes)}")
            await asyncio.sleep(0.1)
            await current_socket.send(raw_bytes)
            await current_socket.send("AUDIO_END")
            logger.info(f"✅ Audio enviado — {len(raw_bytes)} bytes en un solo envío")

        except Exception as e:
            logger.error(f"❌ Error generando/enviando audio: {e}", exc_info=True)
            esp32_websocket = None

async def handle_esp32_quart():
    global esp32_websocket, _img_buffer, _img_total, _img_mode, _collecting_image

    logger.info("📡 Nueva conexión WebSocket entrante")

    # ── Despachar agente ──────────────────────────────────────────────────────
    await _ensure_agent_dispatched()

    # ── Conectar a LiveKit si no está conectado ───────────────────────────────
    if active_room is None:
        logger.info("🔌 LiveKit no conectado — conectando ahora...")
        try:
            await connect_to_livekit()
        except Exception as e:
            logger.error(f"❌ No se pudo conectar a LiveKit: {e}")
            await websocket.send("ERROR:Bridge no listo")
            return

    for i in range(15):
        if active_room is not None:
            break
        logger.warning(f"⏳ Esperando LiveKit {i+1}/15...")
        await asyncio.sleep(1)

    if active_room is None:
        logger.error("❌ LiveKit nunca se conectó — rechazando ESP32")
        await websocket.send("ERROR:Bridge no listo")
        return

    # ── Reemplazar conexión anterior ──────────────────────────────────────────
    if esp32_websocket is not None:
        logger.warning("⚠️ Ya había un ESP32 conectado — reemplazando")
        old = esp32_websocket
        esp32_websocket = None
        try:
            await old.close()
        except Exception:
            pass
        await asyncio.sleep(0.5)

    esp32_websocket = websocket._get_current_object()
    logger.info("✅ ESP32 registrado correctamente")

    # ── Notificar al agente ───────────────────────────────────────────────────
    if active_room:
        try:
            await active_room.local_participant.publish_data(
                b"BRIDGE:connected", reliable=True
            )
            logger.info("📢 Agente notificado: BRIDGE:connected")
        except Exception as e:
            logger.error(f"❌ Error notificando agente: {e}")

    # ── Audio de bienvenida ───────────────────────────────────────────────────
    logger.info("🎙️ Enviando audio de bienvenida...")
    await _generate_and_send_audio("Hola, soy Navi, tu asistente de gafas inteligentes.")

    # ── Heartbeat — mantener viva la conexión en Railway ─────────────────────
    async def _heartbeat():
        logger.info("💓 Heartbeat iniciado")
        while esp32_websocket is not None:
            await asyncio.sleep(15)
            try:
                if esp32_websocket is not None:
                    await esp32_websocket.send("PING")
                    logger.debug("💓 PING enviado")
            except Exception as e:
                logger.warning(f"💔 Heartbeat falló: {e}")
                break
        logger.info("💔 Heartbeat detenido")

    heartbeat_task = asyncio.ensure_future(_heartbeat())

    # ── Loop principal ────────────────────────────────────────────────────────
    logger.info("🔄 Entrando al loop principal de mensajes")
    try:
        while True:
            message = await websocket.receive()

            # ── Datos binarios (chunks de imagen) ────────────────────────────
            if isinstance(message, bytes):
                if _collecting_image:
                    _img_buffer.extend(message)
                    logger.debug(f"[IMG] 📦 Binario recibido: {len(message)} bytes (total: {len(_img_buffer)})")
                else:
                    logger.warning(f"⚠️ Binario recibido fuera de contexto IMG: {len(message)} bytes")
                continue

            # ── Ignorar PONG ──────────────────────────────────────────────────
            if message == "PONG":
                logger.debug("💓 PONG recibido")
                continue

            logger.info(f"[WS] 📨 Mensaje: {message[:100]}")

            # ── HELLO ─────────────────────────────────────────────────────────
            if message.startswith("HELLO:"):
                logger.info(f"👋 ESP32 saludó: {message}")
                await websocket.send(f"STATUS:Conectado a {active_room_name}")
                logger.info("✅ STATUS enviado al ESP32")

            # ── MODE ──────────────────────────────────────────────────────────
            elif message.startswith("MODE:"):
                mode = message.split(":")[1].strip()
                logger.info(f"🔄 Cambio de modo → {mode}")
                if active_room:
                    await active_room.local_participant.publish_data(
                        message.encode(), reliable=True
                    )
                    logger.info(f"📢 Modo enviado al agente: {mode}")
                if mode != "assistant":
                    await _generate_and_send_audio(f"Modo {mode} activado.")

            # ── IMG_START ─────────────────────────────────────────────────────
            elif message.startswith("IMG_START:"):
                parts = message.split(":")
                _img_mode  = parts[1] if len(parts) > 2 else "describe"
                _img_total = int(parts[-1])
                _img_buffer = bytearray()
                _collecting_image = True
                logger.info(f"📷 Imagen iniciada — modo={_img_mode}, esperando {_img_total} bytes")

            # ── IMG_END ───────────────────────────────────────────────────────
            elif message == "IMG_END":
                _collecting_image = False
                received = len(_img_buffer)
                logger.info(f"📷 Imagen completa — {received}/{_img_total} bytes recibidos")

                if received == 0:
                    logger.error("❌ Imagen vacía — descartando")
                elif active_room is None:
                    logger.error("❌ LiveKit no conectado — no se puede enviar imagen")
                else:
                    img_b64 = base64.b64encode(_img_buffer).decode("utf-8")
                    logger.info(f"📷 Imagen codificada en b64: {len(img_b64)} chars — enviando al agente...")
                    try:
                        await active_room.local_participant.publish_data(
                            f"IMG_START:{_img_mode}:0".encode(), reliable=True
                        )
                        await active_room.local_participant.publish_data(
                            f"IMG_CHUNK:0:{img_b64}".encode(), reliable=True
                        )
                        await active_room.local_participant.publish_data(
                            b"IMG_END", reliable=True
                        )
                        logger.info("✅ Imagen enviada al agente LiveKit")
                    except Exception as e:
                        logger.error(f"❌ Error enviando imagen al agente: {e}", exc_info=True)

            # ── OBSTACLE ──────────────────────────────────────────────────────
            elif message.startswith("OBSTACLE:"):
                dist = message.split(":")[1]
                logger.info(f"🚧 Obstáculo detectado: {dist} cm")

                if active_room:
                    try:
                        await active_room.local_participant.publish_data(
                            message.encode(), reliable=True
                        )
                    except Exception as e:
                        logger.error(f"❌ Error enviando OBSTACLE al agente: {e}")

                # Throttle — no alertar si está ocupado o muy seguido
                now = asyncio.get_event_loop().time()
                global _last_obstacle_audio
                if not _audio_lock.locked() and (now - _last_obstacle_audio) > OBSTACLE_AUDIO_COOLDOWN:
                    _last_obstacle_audio = now
                    await _generate_and_send_audio(f"Atención, obstáculo a {dist} centímetros.")
                else:
                    logger.info(f"⏭️ Alerta de obstáculo omitida (cooldown o audio ocupado)")

            else:
                logger.warning(f"[WS] ❓ Mensaje desconocido: {message[:80]}")

    except Exception as e:
        logger.error(f"❌ Error en loop principal: {e}", exc_info=True)
    finally:
        heartbeat_task.cancel()
        esp32_websocket = None
        _collecting_image = False
        logger.info("📡 ESP32 desconectado — limpieza completa")