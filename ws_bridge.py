from __future__ import annotations
import asyncio
import base64
import logging
import os

import edge_tts
import httpx
from quart import websocket
from livekit import rtc
from dotenv import load_dotenv
from prompts import MODE_TTS, WELCOME_MESSAGE

load_dotenv()

# Nombre de sala para API + RTC (debe estar definido antes de dispatch / connect)
_DEFAULT_ROOM = os.getenv("DEFAULT_ROOM", "gafas-test")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ws-bridge")

active_room: rtc.Room | None = None
active_room_name: str | None = _DEFAULT_ROOM
esp32_websocket = None
_audio_lock = asyncio.Lock()
_reconnecting = False

_img_buffer       = bytearray()
_img_total        = 0
_img_mode         = "describe"
_collecting_image = False

# ESP32 → bridge: WAV (PCM16 mono common) chunked like images
_collecting_record = False
_record_buffer     = bytearray()
_record_expected   = 0

# Throttle alertas de obstáculo
_last_obstacle_audio = 0.0
OBSTACLE_AUDIO_COOLDOWN = 4.0
_last_tts_text = ""
_last_tts_ts = 0.0
TTS_DEDUP_WINDOW_S = 2.5
_dispatch_lock = asyncio.Lock()
_dispatch_ready = False
_last_connected_tts_ts = 0.0
CONNECTED_TTS_COOLDOWN_S = 30.0
_tts_pending: list[str] = []
_welcome_sent_for_session = False

# ─────────────────────────────────────────────────────────────────────────────
#  LiveKit helpers
# ─────────────────────────────────────────────────────────────────────────────

async def safe_publish_data(payload: bytes, *, reliable: bool = True) -> bool:
    """Publica datos en la sala; no tumba el handler si el motor RTC está cerrado."""
    global active_room
    if active_room is None:
        return False
    try:
        await active_room.local_participant.publish_data(payload, reliable=reliable)
        return True
    except Exception as e:
        logger.warning(f"⚠️ publish_data omitido (LiveKit): {e}")
        return False


async def _ensure_livekit_room_exists():
    """Crea la sala en el servidor si no existe (dispatch falla con 404 si falta)."""
    from livekit.api import LiveKitAPI, CreateRoomRequest

    room_name = active_room_name or _DEFAULT_ROOM
    lk = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    try:
        await lk.room.create_room(CreateRoomRequest(name=room_name))
        logger.info(f"✅ Sala LiveKit disponible: {room_name}")
    except Exception as e:
        msg = str(e).lower()
        if "already exists" in msg or "409" in msg or "duplicate" in msg:
            logger.debug(f"Sala ya existía: {room_name}")
        else:
            logger.warning(f"⚠️ create_room ({room_name}): {e}")
    finally:
        await lk.aclose()


async def _ensure_agent_dispatched():
    global _dispatch_ready
    if _dispatch_ready:
        return
    async with _dispatch_lock:
        if _dispatch_ready:
            return

    from livekit.api import LiveKitAPI
    from livekit import api

    logger.info("🤖 Verificando dispatch del agente...")
    room_name = active_room_name or _DEFAULT_ROOM
    lk = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    try:
        existing = await lk.agent_dispatch.list_dispatch(room_name=room_name)
        dispatches = getattr(existing, "agent_dispatches", [])
        if dispatches:
            logger.info(f"⚡ Agente ya despachado ({len(dispatches)} dispatch activo)")
            _dispatch_ready = True
            return
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="smart-glasses",
                room=room_name,
            )
        )
        logger.info("✅ Dispatch del agente creado")
        _dispatch_ready = True
    except Exception as e:
        logger.error(f"❌ Error creando dispatch: {e}")
    finally:
        await lk.aclose()


async def _transcribe_wav_and_publish_to_agent(wav_bytes: bytes):
    """ESP32 envía WAV de prueba o micrófono futuro → Groq Whisper → agente."""
    if not wav_bytes or len(wav_bytes) < 200:
        logger.warning(f"⚠️ WAV demasiado corto ({len(wav_bytes) if wav_bytes else 0} B)")
        return
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        logger.error("❌ GROQ_API_KEY falta — no se puede transcribir")
        return
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {groq_key}"},
                files={"file": ("esp32.wav", wav_bytes, "audio/wav")},
                data={
                    "model": "whisper-large-v3-turbo",
                    "language": "es",
                },
            )
        r.raise_for_status()
        text = r.json().get("text", "").strip()
        logger.info(f"🎙️ Whisper: '{text}'")
        if text:
            await safe_publish_data(f"USER_UTTERANCE:{text}".encode("utf-8"))
        else:
            logger.warning("⚠️ Whisper devolvió texto vacío")
            await _generate_and_send_audio(
                "No se escuchó nada en el micrófono."
            )
    except Exception as e:
        logger.error(f"❌ Transcripción / envío agente: {e}", exc_info=True)
        await _generate_and_send_audio("Error al procesar el audio del micrófono.")


async def connect_to_livekit():
    global active_room, active_room_name
    from livekit.api import AccessToken, VideoGrants

    active_room_name = _DEFAULT_ROOM
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
        global _last_tts_text, _last_tts_ts
        try:
            msg = packet.data.decode("utf-8")
            logger.info(f"📨 LiveKit→Bridge: {msg[:80]}")
            if msg.startswith("TTS:"):
                text = msg[4:]
                now = asyncio.get_event_loop().time()
                if text == _last_tts_text and (now - _last_tts_ts) < TTS_DEDUP_WINDOW_S:
                    logger.info(f"🔁 TTS duplicado ignorado: '{text[:60]}'")
                    return
                _last_tts_text = text
                _last_tts_ts = now
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
    global esp32_websocket, _tts_pending

    current_socket = esp32_websocket
    if current_socket is None:
        logger.warning(f"⚠️ TTS ignorado — ESP32 no conectado: '{text[:50]}'")
        return

    if _audio_lock.locked():
        logger.warning(f"⚠️ Audio en curso — en cola ({len(_tts_pending)+1}): '{text[:50]}'")
        _tts_pending.append(text)
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

                # Si cambió el socket (reconexión CAM), detener stream viejo.
                if esp32_websocket is None or esp32_websocket is not current_socket:
                    logger.warning("⚠️ Socket ESP32 cambió/desconectó durante streaming")
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
            # No poner esp32_websocket = None aquí: la CAM puede seguir conectada
            # y el siguiente TTS fallaría con "ESP32 no conectado".
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

    if _tts_pending:
        nxt = _tts_pending.pop(0)
        logger.info(f"🔊 Reproduciendo TTS en cola ({len(_tts_pending)} restantes): '{nxt[:50]}'")
        asyncio.ensure_future(_generate_and_send_audio(nxt))


async def _maybe_send_welcome_tts():
    """Bienvenida una vez por sesión de bridge (el agente también la publica)."""
    global _welcome_sent_for_session
    if _welcome_sent_for_session:
        logger.debug("Bienvenida ya enviada esta sesión")
        return
    _welcome_sent_for_session = True
    logger.info(f"👋 TTS bienvenida ({len(WELCOME_MESSAGE)} chars)")
    await _generate_and_send_audio(WELCOME_MESSAGE)


# ─────────────────────────────────────────────────────────────────────────────
#  Handler principal WebSocket (Quart)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_esp32_quart():
    global esp32_websocket, _img_buffer, _img_total, _img_mode, _collecting_image
    global _collecting_record, _record_buffer, _record_expected
    global _last_connected_tts_ts

    logger.info("📡 Nueva conexión WebSocket entrante (ESP32-CAM)")

    # Sala debe existir en LiveKit Cloud antes de agent dispatch (evita 404)
    await _ensure_livekit_room_exists()
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

    my_socket = websocket._get_current_object()
    esp32_websocket = my_socket
    global _welcome_sent_for_session
    _welcome_sent_for_session = False
    logger.info("✅ ESP32 registrado")

    if not await safe_publish_data(b"BRIDGE:connected"):
        logger.warning("⚠️ No se pudo notificar BRIDGE:connected al agente")

    # La bienvenida se envía al recibir PEER_READY (Dev TCP conectado a la CAM).

    # ── Heartbeat ─────────────────────────────────────────────────────────────
    async def _heartbeat():
        while True:
            await asyncio.sleep(15)
            if esp32_websocket is None:
                break
            try:
                await esp32_websocket.send("PING")
            except Exception as e:
                logger.debug(f"Heartbeat send: {e}")

    heartbeat_task = asyncio.ensure_future(_heartbeat())

    # ── Loop principal ─────────────────────────────────────────────────────────
    try:
        while True:
            message = await websocket.receive()

            # Datos binarios — chunks JPEG (IMG_* ) o WAV (RECORD_*)
            if isinstance(message, bytes):
                if _collecting_image:
                    _img_buffer.extend(message)
                elif _collecting_record:
                    _record_buffer.extend(message)
                else:
                    logger.warning(f"⚠️ BIN fuera de contexto: {len(message)} bytes")
                continue

            if message == "PONG":
                continue

            logger.info(f"[WS] 📨 {message[:100]}")

            # ── HELLO ─────────────────────────────────────────────────────────
            if message.startswith("HELLO:"):
                await websocket.send(f"STATUS:Conectado a {active_room_name}")

            elif message == "PEER_READY":
                logger.info("🔊 CAM: Dev audio TCP listo — TTS bienvenida")
                asyncio.ensure_future(_generate_and_send_audio(WELCOME_MESSAGE))

            # ── MODE ──────────────────────────────────────────────────────────
            elif message.startswith("MODE:"):
                mode = message.split(":")[1].strip().lower()
                await safe_publish_data(message.encode())
                tts = MODE_TTS.get(mode, f"Modo {mode} activado.")
                logger.info(f"[WS] MODE:{mode} → TTS inmediato ({len(tts)} chars)")
                asyncio.ensure_future(_generate_and_send_audio(tts))

            # ── IMG_START ─────────────────────────────────────────────────────
            elif message.startswith("IMG_START:"):
                parts = message.split(":")
                _collecting_record = False
                _record_buffer = bytearray()
                _img_mode  = parts[1] if len(parts) > 2 else "describe"
                _img_total = int(parts[-1])
                _img_buffer = bytearray()
                _collecting_image = True

            # ── IMG_END ───────────────────────────────────────────────────────
            elif message == "IMG_END":
                _collecting_image = False
                received = len(_img_buffer)
                logger.info(f"📷 Imagen: {received}/{_img_total} bytes")

                if received > 0:
                    img_b64 = base64.b64encode(_img_buffer).decode("utf-8")
                    ok = (
                        await safe_publish_data(f"IMG_START:{_img_mode}:0".encode())
                        and await safe_publish_data(f"IMG_CHUNK:0:{img_b64}".encode())
                        and await safe_publish_data(b"IMG_END")
                    )
                    if ok:
                        logger.info("✅ Imagen enviada al agente")
                    else:
                        logger.warning("⚠️ Imagen no enviada — LiveKit no disponible")
                        await _generate_and_send_audio(
                            "No pude enviar la imagen al servidor."
                        )

            # ── RECORD_* — ESP32→bridge WAV ─────────────────────────────────
            elif message.startswith("RECORD_START:"):
                _collecting_image = False
                _img_buffer = bytearray()
                _record_expected = int(message.split(":")[1].strip())
                _record_buffer = bytearray()
                _collecting_record = True
                logger.info(f"🎙️ RECORD_START esperando {_record_expected} B WAV")

            elif message == "RECORD_END":
                _collecting_record = False
                got = len(_record_buffer)
                logger.info(f"🎙️ RECORD_END — recibidos {got}/{_record_expected} B")
                wav_copy = bytes(_record_buffer)
                _record_buffer = bytearray()
                asyncio.ensure_future(_transcribe_wav_and_publish_to_agent(wav_copy))

            # ── OBSTACLE ──────────────────────────────────────────────────────
            elif message.startswith("OBSTACLE:"):
                dist = message.split(":")[1]
                await safe_publish_data(message.encode())

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
        if esp32_websocket is my_socket:
            esp32_websocket = None
            logger.info("📡 ESP32 desconectado — limpieza completa")
        _collecting_image = False
        _collecting_record = False
        _record_buffer = bytearray()