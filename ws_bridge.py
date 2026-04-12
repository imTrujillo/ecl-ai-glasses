from __future__ import annotations
import asyncio
import base64
import logging
import os
import edge_tts
from quart import websocket
from livekit import rtc
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
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

# Throttle alertas de obstáculo
_last_obstacle_audio = 0.0
OBSTACLE_AUDIO_COOLDOWN = 4.0

# ─────────────────────────────────────────────────────────────────────────────
#  LiveKit helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _ensure_agent_dispatched():
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
                room=active_room_name,
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
        AccessToken(os.getenv("LIVEKIT_API_KEY"), os.getenv("LIVEKIT_API_SECRET"))
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
                logger.info(f"🔊 TTS recibido: '{text[:60]}'")
                asyncio.ensure_future(_generate_and_send_audio(text))
        except Exception as e:
            logger.error(f"❌ Bridge data error: {e}")

    @room.on("disconnected")
    def on_disconnected(*args, **kwargs):
        logger.warning("⚠️ LiveKit desconectado — iniciando reconexión")
        asyncio.ensure_future(_reconnect_livekit())

    await room.connect(os.getenv("LIVEKIT_URL"), token.to_jwt())
    active_room = room
    logger.info(f"✅ Bridge conectado — sala: {active_room_name}")


async def _reconnect_livekit():
    global active_room, _reconnecting
    if _reconnecting:
        return
    _reconnecting = True
    active_room = None
    delay = 5
    while True:
        await asyncio.sleep(delay)
        logger.info("🔄 Intentando reconectar LiveKit...")
        try:
            await connect_to_livekit()
            logger.info("✅ Bridge reconectado")
            _reconnecting = False
            return
        except Exception as e:
            delay = min(delay * 2, 60)
            logger.error(f"❌ Reconexión fallida (próximo en {delay}s): {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  AUDIO STREAMING — arquitectura nueva
#
#  Pipeline:  edge-tts  ──MP3 chunks──►  ffmpeg stdin
#                                              │
#                                        ffmpeg stdout
#                                              │
#                                       RAW PCM u8 8kHz
#                                              │
#                                    WebSocket binario ──► ESP32 ring buffer ──► DAC
#
#  Protocolo nuevo:
#    Server → ESP32 :  "AUDIO_STREAM"          ← inicio (sin tamaño)
#    Server → ESP32 :  <bytes binarios>         ← chunks 2 KB mientras llegan
#    Server → ESP32 :  "AUDIO_END"             ← fin del stream
# ─────────────────────────────────────────────────────────────────────────────

async def _generate_and_send_audio(text: str):
    global esp32_websocket

    current_socket = esp32_websocket
    if current_socket is None:
        logger.warning("⚠️ TTS ignorado — ESP32 no conectado")
        return

    if _audio_lock.locked():
        logger.warning(f"⚠️ Audio en curso — descartando: '{text[:40]}'")
        return

    async with _audio_lock:
        proc = None
        try:
            logger.info(f"🎤 Iniciando TTS streaming: '{text[:60]}'")

            # ── Arrancar ffmpeg: lee MP3 de stdin, escribe RAW u8 8kHz a stdout ──
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

            # ── Task: alimenta edge-tts → ffmpeg stdin concurrentemente ──────────
            async def _feed_ffmpeg():
                try:
                    communicate = edge_tts.Communicate(text, "es-PY-TaniaNeural")
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            proc.stdin.write(chunk["data"])
                    # Vaciar buffer y cerrar — ffmpeg procesa lo restante y termina
                    await proc.stdin.drain()
                except Exception as e:
                    logger.error(f"❌ Error alimentando ffmpeg: {e}")
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass

            feed_task = asyncio.ensure_future(_feed_ffmpeg())

            # ── Verificar socket antes de empezar ─────────────────────────────────
            if esp32_websocket is None:
                feed_task.cancel()
                return

            # ── Señal de inicio al ESP32 ──────────────────────────────────────────
            await current_socket.send("AUDIO_STREAM")
            logger.info("📡 AUDIO_STREAM enviado — comenzando chunks binarios...")

            # ── Leer ffmpeg stdout y reenviar al ESP32 en tiempo real ─────────────
            CHUNK_SIZE = 2048   # 2 KB ≈ 250 ms @ 8 kHz → latencia baja
            total_sent = 0

            while True:
                raw_chunk = await proc.stdout.read(CHUNK_SIZE)
                if not raw_chunk:
                    break   # ffmpeg terminó

                if esp32_websocket is None:
                    logger.warning("⚠️ ESP32 desconectado durante streaming")
                    break

                await current_socket.send(raw_chunk)
                total_sent += len(raw_chunk)

                # Yield para no bloquear el event loop (heartbeat, mensajes WS, etc.)
                await asyncio.sleep(0)

            await feed_task
            await proc.wait()

            if esp32_websocket is not None:
                await current_socket.send("AUDIO_END")
                logger.info(f"✅ Streaming completo — {total_sent} bytes enviados")
            else:
                logger.warning("⚠️ ESP32 desconectado antes de AUDIO_END")

        except Exception as e:
            logger.error(f"❌ Error en streaming de audio: {e}", exc_info=True)
            esp32_websocket = None
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
#  Handler principal WebSocket (Quart)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_esp32_quart():
    global esp32_websocket, _img_buffer, _img_total, _img_mode, _collecting_image

    logger.info("📡 Nueva conexión WebSocket entrante")

    # Despachar agente si no existe
    await _ensure_agent_dispatched()

    # Conectar a LiveKit si no está conectado
    if active_room is None:
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

    # Reemplazar conexión ESP32 anterior si existe
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
    logger.info("✅ ESP32 registrado")

    # Notificar al agente
    if active_room:
        try:
            await active_room.local_participant.publish_data(b"BRIDGE:connected", reliable=True)
        except Exception as e:
            logger.error(f"❌ Error notificando agente: {e}")

    # Audio de conexión — usa el nuevo streaming
    await _generate_and_send_audio("Conectado.")

    # ── Heartbeat ─────────────────────────────────────────────────────────────
    async def _heartbeat():
        while esp32_websocket is not None:
            await asyncio.sleep(15)
            try:
                if esp32_websocket is not None:
                    await esp32_websocket.send("PING")
            except Exception as e:
                logger.warning(f"💔 Heartbeat falló: {e}")
                break

    heartbeat_task = asyncio.ensure_future(_heartbeat())

    # ── Loop principal ─────────────────────────────────────────────────────────
    try:
        while True:
            message = await websocket.receive()

            # Datos binarios — solo pueden ser chunks de imagen
            if isinstance(message, bytes):
                if _collecting_image:
                    _img_buffer.extend(message)
                else:
                    logger.warning(f"⚠️ BIN fuera de contexto: {len(message)} bytes")
                continue

            if message == "PONG":
                continue

            logger.info(f"[WS] 📨 {message[:100]}")

            # ── HELLO ─────────────────────────────────────────────────────────
            if message.startswith("HELLO:"):
                await websocket.send(f"STATUS:Conectado a {active_room_name}")

            # ── MODE ──────────────────────────────────────────────────────────
            elif message.startswith("MODE:"):
                mode = message.split(":")[1].strip()
                if active_room:
                    await active_room.local_participant.publish_data(
                        message.encode(), reliable=True
                    )
                if mode != "assistant":
                    await _generate_and_send_audio(f"Modo {mode} activado.")

            # ── IMG_START ─────────────────────────────────────────────────────
            elif message.startswith("IMG_START:"):
                parts = message.split(":")
                _img_mode  = parts[1] if len(parts) > 2 else "describe"
                _img_total = int(parts[-1])
                _img_buffer = bytearray()
                _collecting_image = True

            # ── IMG_END ───────────────────────────────────────────────────────
            elif message == "IMG_END":
                _collecting_image = False
                received = len(_img_buffer)
                logger.info(f"📷 Imagen: {received}/{_img_total} bytes")

                if received > 0 and active_room:
                    img_b64 = base64.b64encode(_img_buffer).decode("utf-8")
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
                        logger.info("✅ Imagen enviada al agente")
                    except Exception as e:
                        logger.error(f"❌ Error enviando imagen: {e}")

            # ── OBSTACLE ──────────────────────────────────────────────────────
            elif message.startswith("OBSTACLE:"):
                dist = message.split(":")[1]
                if active_room:
                    try:
                        await active_room.local_participant.publish_data(
                            message.encode(), reliable=True
                        )
                    except Exception as e:
                        logger.error(f"❌ Error enviando OBSTACLE: {e}")

                now = asyncio.get_event_loop().time()
                global _last_obstacle_audio
                if not _audio_lock.locked() and (now - _last_obstacle_audio) > OBSTACLE_AUDIO_COOLDOWN:
                    _last_obstacle_audio = now
                    await _generate_and_send_audio(f"Atención, obstáculo a {dist} centímetros.")

            else:
                logger.warning(f"[WS] ❓ Desconocido: {message[:80]}")

    except Exception as e:
        logger.error(f"❌ Error en loop principal: {e}", exc_info=True)
    finally:
        heartbeat_task.cancel()
        esp32_websocket = None
        _collecting_image = False
        logger.info("📡 ESP32 desconectado — limpieza completa")