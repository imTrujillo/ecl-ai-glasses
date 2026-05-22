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
from prompts import (
    INSTRUCTIONS,
    WELCOME_MESSAGE,
    MODE_TTS,
    MODE_OCR,
    MODE_DESCRIBE,
    MODE_ASSISTANT,
)

load_dotenv()

# 0 = nunca RTC (solo Groq+TTS en el bridge). 1 = intentar LiveKit para agente/imágenes.
_USE_LIVEKIT = os.getenv("NAVI_USE_LIVEKIT", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
_livekit_disabled = False
_livekit_connect_lock = asyncio.Lock()

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
# Varios paneles (PC + móvil ngrok) a la vez — antes solo uno y se pisaban.
phone_clients: dict[int, dict] = {}
_bridge_mode = "assistant"
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
# Teléfono / web — grabación WAV e imágenes (estado aparte del ESP32)
_phone_collecting_record = False
_phone_record_buffer = bytearray()
_phone_record_expected = 0
_phone_collecting_image = False
_phone_img_buffer = bytearray()
_phone_img_mode = "describe"
_phone_img_total = 0

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


def _phone_register(ws, *, alerts: str = "local") -> int:
    """Registra panel web; alerts=server → TTS corto para modos/obstáculos (móvil)."""
    pid = id(ws)
    phone_clients[pid] = {"ws": ws, "alerts": alerts, "welcomed": False}
    logger.info(
        f"📱 Panel registrado ({len(phone_clients)} activo(s), alertas={alerts})"
    )
    return pid


def _phone_unregister(ws) -> None:
    pid = id(ws)
    if pid in phone_clients:
        phone_clients.pop(pid, None)
        logger.info(f"📱 Panel desregistrado ({len(phone_clients)} restante(s))")


async def _send_phone(ws, text: str) -> None:
    try:
        await ws.send(text)
    except Exception as e:
        logger.debug(f"send_phone: {e}")


async def _broadcast_phones(text: str, *, exclude_id: int | None = None) -> None:
    """Envía texto a todos los paneles (sincroniza PC ↔ móvil)."""
    dead: list[int] = []
    for pid, client in list(phone_clients.items()):
        if exclude_id is not None and pid == exclude_id:
            continue
        try:
            await client["ws"].send(text)
        except Exception:
            dead.append(pid)
    for pid in dead:
        phone_clients.pop(pid, None)


async def _notify_phone(text: str) -> None:
    await _broadcast_phones(text)


def _audio_targets(*, phone_scope: str = "all"):
    """
    phone_scope:
      all — respuestas IA / bienvenida a todos los paneles (+ ESP si hay)
      server-alerts — solo paneles que pidieron TTS para modo/obstáculo (móvil)
    """
    out = []
    if phone_scope in ("all", "server-alerts"):
        for client in phone_clients.values():
            if phone_scope == "server-alerts" and client["alerts"] != "server":
                continue
            out.append(client["ws"])
    if phone_scope == "all" and esp32_websocket is not None:
        out.append(esp32_websocket)
    return out


async def _sync_state_to_phone(ws) -> None:
    """Estado compartido al conectar un panel nuevo."""
    esp = "ESP32:online" if esp32_websocket is not None else "ESP32:offline"
    await _send_phone(ws, esp)
    await _send_phone(ws, f"MODE:{_bridge_mode}")
    await _send_phone(ws, f"PANELS:{len(phone_clients)}")


async def _apply_bridge_mode(
    mode: str,
    *,
    source: str,
    exclude_phone_id: int | None = None,
) -> None:
    """Un solo modo para ESP32 + todos los paneles (no hay salas distintas por dispositivo)."""
    global _bridge_mode
    mode = mode.strip().lower()
    if mode not in ("assistant", "ocr", "describe"):
        mode = "assistant"
    _bridge_mode = mode
    msg = f"MODE:{mode}"
    # Sala LiveKit = bus de sincronización (todos los paneles en la misma room).
    if source != "lk":
        await safe_publish_data(msg.encode())
    await _broadcast_phones(msg, exclude_id=exclude_phone_id)
    if esp32_websocket is not None and source != "esp32":
        try:
            await esp32_websocket.send(msg)
        except Exception as e:
            logger.debug(f"MODE → ESP32: {e}")
    if any(c["alerts"] == "server" for c in phone_clients.values()):
        tts = MODE_TTS.get(mode, f"Modo {mode} activado.")
        asyncio.ensure_future(
            _generate_and_send_audio(tts, phone_scope="server-alerts")
        )
    logger.info(f"Modo bridge → {mode} (origen={source}, sala={active_room_name})")


async def _relay_obstacle(dist: str, *, source: str) -> None:
    """Obstáculo: publicar en la sala y avisar paneles (voz local o TTS según cliente)."""
    global _last_obstacle_audio
    msg = f"OBSTACLE:{dist}"
    if source != "lk":
        await safe_publish_data(msg.encode())
    await _broadcast_phones(msg)
    try:
        cm = float(dist)
    except ValueError:
        return
    if cm <= 0:
        return
    if not any(c["alerts"] == "server" for c in phone_clients.values()):
        return
    now = asyncio.get_event_loop().time()
    if _audio_lock.locked() or (now - _last_obstacle_audio) <= OBSTACLE_AUDIO_COOLDOWN:
        return
    _last_obstacle_audio = now
    asyncio.ensure_future(
        _generate_and_send_audio(
            f"Obstáculo a {int(round(cm))} centímetros.",
            phone_scope="server-alerts",
        )
    )


ESP32_PING_INTERVAL_S = 3.0
ESP32_PONG_TIMEOUT_S = 7.0


async def _sync_esp32_to_room(online: bool) -> None:
    """Estado de gafas: primero paneles (UI rápida), luego sala LiveKit."""
    msg = "ESP32:online" if online else "ESP32:offline"
    await _broadcast_phones(msg)
    asyncio.ensure_future(safe_publish_data(msg.encode()))


async def _esp32_detach(sock, *, reason: str = "") -> None:
    """Marca gafas offline y avisa paneles de inmediato."""
    global esp32_websocket
    if esp32_websocket is not sock:
        return
    esp32_websocket = None
    extra = f" ({reason})" if reason else ""
    logger.info(f"📡 ESP32 desconectado{extra}")
    await _broadcast_phones("ESP32:offline")
    asyncio.ensure_future(safe_publish_data(b"ESP32:offline"))


async def _groq_chat_reply(user_text: str) -> str:
    """Respuesta LLM directa (sin LiveKit) para voz del teléfono o fallback."""
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        return "Falta configurar GROQ_API_KEY en el servidor."
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": INSTRUCTIONS},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 200,
                "temperature": 0.5,
            },
        )
    r.raise_for_status()
    data = r.json()
    if "choices" not in data:
        logger.error(f"Groq chat sin choices: {data}")
        return "No pude responder, intenta de nuevo."
    reply = data["choices"][0]["message"]["content"].strip()
    return reply or "No tengo respuesta para eso."


async def _groq_process_image(image_b64: str, mode: str) -> str:
    mode_prompts = {
        "ocr": MODE_OCR,
        "describe": MODE_DESCRIBE,
        "assistant": MODE_ASSISTANT,
    }
    instruction = mode_prompts.get(mode, MODE_DESCRIBE)
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        return "Falta GROQ_API_KEY para analizar imágenes."
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}"},
            json={
                "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instruction},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_b64}"
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 300,
            },
        )
    r.raise_for_status()
    data = r.json()
    if "choices" not in data:
        return "No pude procesar la imagen."
    return data["choices"][0]["message"]["content"].strip() or "No veo nada claro."


async def _transcribe_wav_and_publish_to_agent(wav_bytes: bytes, *, source: str = "esp32"):
    if not wav_bytes or len(wav_bytes) < 200:
        logger.warning(f"⚠️ WAV demasiado corto ({len(wav_bytes) if wav_bytes else 0} B)")
        return
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        logger.error("❌ GROQ_API_KEY falta — no se puede transcribir")
        await _generate_and_send_audio("El servidor no tiene clave de Groq configurada.")
        return
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {groq_key}"},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={
                    "model": "whisper-large-v3-turbo",
                    "language": "es",
                },
            )
        r.raise_for_status()
        text = r.json().get("text", "").strip()
        logger.info(f"🎙️ Whisper ({source}): '{text}'")
        await _notify_phone(f"TRANSCRIPT:{text}")
        if not text:
            await _notify_phone("EVENT:no-audio")
            await _generate_and_send_audio("No se escuchó nada en el micrófono.")
            return
        if active_room is not None:
            ok = await safe_publish_data(f"USER_UTTERANCE:{text}".encode("utf-8"))
            if ok:
                return
            logger.warning("LiveKit sin publish — respuesta directa Groq")
        await _notify_phone("EVENT:thinking")
        reply = await _groq_chat_reply(text)
        await _notify_phone(f"REPLY:{reply}")
        await _generate_and_send_audio(reply)
    except Exception as e:
        logger.error(f"❌ Transcripción / respuesta: {e}", exc_info=True)
        await _notify_phone("EVENT:error-audio")
        await _generate_and_send_audio("Error al procesar el audio del micrófono.")


async def connect_to_livekit():
    global active_room, active_room_name, _livekit_disabled
    from livekit.api import AccessToken, VideoGrants

    if _livekit_disabled or not _USE_LIVEKIT:
        raise RuntimeError("LiveKit deshabilitado (NAVI_USE_LIVEKIT=0 o cuota agotada)")

    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    lk_url = os.getenv("LIVEKIT_URL", "").strip()
    if not api_key or not api_secret or not lk_url:
        raise RuntimeError(
            "Faltan LIVEKIT_URL, LIVEKIT_API_KEY o LIVEKIT_API_SECRET en .env"
        )

    active_room_name = _DEFAULT_ROOM
    logger.info(f"🔌 Conectando a LiveKit — sala: {active_room_name}")

    token = (
        AccessToken(api_key, api_secret)
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
            logger.info(f"📨 LiveKit sala ({active_room_name}): {msg[:80]}")
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
                return
            if msg.startswith("MODE:") or msg.startswith("NAVI:MODE:"):
                mode = msg.split(":")[-1].strip().lower()
                asyncio.ensure_future(_apply_bridge_mode(mode, source="lk"))
                return
            if msg.startswith("OBSTACLE:") or msg.startswith("NAVI:OBSTACLE:"):
                dist = msg.split(":")[-1].strip()
                asyncio.ensure_future(_relay_obstacle(dist, source="lk"))
                return
            if msg in ("NAVI:ESP32:online", "ESP32:online"):
                asyncio.ensure_future(_broadcast_phones("ESP32:online"))
                return
            if msg in ("NAVI:ESP32:offline", "ESP32:offline"):
                asyncio.ensure_future(_broadcast_phones("ESP32:offline"))
        except Exception as e:
            logger.error(f"❌ Bridge data error: {e}")

    @room.on("disconnected")
    def on_disconnected(*args, **kwargs):
        logger.warning("⚠️ LiveKit desconectado — iniciando reconexión")
        asyncio.ensure_future(_reconnect_livekit())

    await room.connect(lk_url, token.to_jwt())
    active_room = room
    logger.info(f"✅ Bridge conectado — sala: {active_room_name}")


async def ensure_livekit_connected(*, required: bool = False) -> bool:
    """Una sola conexión RTC compartida; evita 429 por múltiples intentos."""
    global active_room, _livekit_disabled

    if _livekit_disabled or not _USE_LIVEKIT:
        return False
    if active_room is not None:
        return True

    async with _livekit_connect_lock:
        if active_room is not None:
            return True
        if _livekit_disabled:
            return False
        try:
            await _ensure_livekit_room_exists()
            await _ensure_agent_dispatched()
            await connect_to_livekit()
            return active_room is not None
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "limit" in err or "401" in err or "unauthorized" in err:
                _livekit_disabled = True
                logger.error(
                    "❌ LiveKit no disponible (%s). "
                    "Modo local: voz/imagen vía Groq. "
                    "Para forzar sin LiveKit: NAVI_USE_LIVEKIT=0 en .env",
                    e,
                )
            elif required:
                logger.error(f"❌ LiveKit requerido pero falló: {e}")
            else:
                logger.warning(f"⚠️ LiveKit opcional no conectó: {e}")
            return False


async def _reconnect_livekit():
    global active_room, _reconnecting
    if _reconnecting or _livekit_disabled:
        return
    _reconnecting = True
    active_room = None
    delay = 5
    while not _livekit_disabled:
        await asyncio.sleep(delay)
        logger.info("🔄 Intentando reconectar LiveKit...")
        try:
            await connect_to_livekit()
            logger.info("✅ Bridge reconectado")
            _reconnecting = False
            return
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "401" in err:
                _livekit_disabled = True
                _reconnecting = False
                logger.error("❌ LiveKit deshabilitado tras error de cuota/auth")
                return
            delay = min(delay * 2, 60)
            logger.error(f"❌ Reconexión fallida (próximo en {delay}s): {e}")
    _reconnecting = False


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

async def _generate_and_send_audio(text: str, *, phone_scope: str = "all"):
    global _tts_pending

    targets = _audio_targets(phone_scope=phone_scope)
    if not targets:
        logger.warning(f"⚠️ TTS sin oyentes — en cola: '{text[:50]}'")
        _tts_pending.append(text)
        return

    if _audio_lock.locked():
        logger.warning(f"⚠️ Audio en curso — en cola ({len(_tts_pending)+1}): '{text[:50]}'")
        _tts_pending.append(text)
        return

    async with _audio_lock:
        proc = None
        try:
            logger.info(
                f"🎤 TTS streaming ({len(targets)} oyente(s)): '{text[:60]}'"
            )
            await _notify_phone(f"TTS_TEXT:{text[:200]}")

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

            async def _feed_ffmpeg():
                try:
                    communicate = edge_tts.Communicate(text, "es-PY-TaniaNeural")
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            proc.stdin.write(chunk["data"])
                    await proc.stdin.drain()
                except Exception as e:
                    logger.error(f"❌ Error alimentando ffmpeg: {e}")
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass

            feed_task = asyncio.ensure_future(_feed_ffmpeg())

            targets = _audio_targets(phone_scope=phone_scope)
            if not targets:
                feed_task.cancel()
                return

            for sock in targets:
                await sock.send("AUDIO_STREAM")

            CHUNK_SIZE = 2048
            total_sent = 0

            while True:
                raw_chunk = await proc.stdout.read(CHUNK_SIZE)
                if not raw_chunk:
                    break

                targets = _audio_targets(phone_scope=phone_scope)
                if not targets:
                    break

                for sock in targets:
                    try:
                        await sock.send(raw_chunk)
                    except Exception as e:
                        logger.debug(f"chunk send: {e}")
                total_sent += len(raw_chunk)
                await asyncio.sleep(0)

            await feed_task
            await proc.wait()

            targets = _audio_targets(phone_scope=phone_scope)
            for sock in targets:
                try:
                    await sock.send("AUDIO_END")
                except Exception:
                    pass
            logger.info(f"✅ TTS completo — {total_sent} bytes PCM u8")

        except Exception as e:
            logger.error(f"❌ Error en streaming de audio: {e}", exc_info=True)
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

    if _tts_pending:
        nxt = _tts_pending.pop(0)
        logger.info(f"🔊 TTS en cola ({len(_tts_pending)} restantes): '{nxt[:50]}'")
        asyncio.ensure_future(_generate_and_send_audio(nxt, phone_scope=phone_scope))


async def _maybe_send_welcome_tts():
    """Bienvenida una vez por sesión de bridge (el agente también la publica)."""
    global _welcome_sent_for_session
    if _welcome_sent_for_session:
        logger.debug("Bienvenida ya enviada esta sesión")
        return
    _welcome_sent_for_session = True
    logger.info(f"👋 TTS bienvenida ({len(WELCOME_MESSAGE)} chars)")
    await _generate_and_send_audio(WELCOME_MESSAGE)


async def _img_reply_groq(image_b64: str, mode: str) -> None:
    try:
        await _notify_phone(f"EVENT:analyzing:{mode}")
        reply = await _groq_process_image(image_b64, mode)
        await _notify_phone(f"REPLY:{reply}")
        await _generate_and_send_audio(reply)
    except Exception as e:
        logger.error(f"❌ Imagen Groq: {e}", exc_info=True)
        await _notify_phone("EVENT:error-image")
        await _generate_and_send_audio("No pude analizar la foto.")


# ─────────────────────────────────────────────────────────────────────────────
#  Handler principal WebSocket (Quart)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_esp32_quart():
    global esp32_websocket, _img_buffer, _img_total, _img_mode, _collecting_image
    global _collecting_record, _record_buffer, _record_expected
    global _last_connected_tts_ts

    logger.info("📡 Nueva conexión WebSocket entrante (ESP32-CAM)")

    lk_ok = await ensure_livekit_connected(required=False)
    if not lk_ok:
        logger.warning(
            "⚠️ ESP32 sin LiveKit — modos, obstáculos y fotos vía Groq directo"
        )

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
    esp32_last_pong = asyncio.get_event_loop().time()
    logger.info("✅ ESP32 registrado")
    await websocket.send("STATUS:ESP32 registrado en el servidor")
    await _sync_esp32_to_room(True)

    if lk_ok:
        if not await safe_publish_data(b"BRIDGE:connected"):
            logger.warning("⚠️ No se pudo notificar BRIDGE:connected al agente")

    async def _welcome_fallback():
        await asyncio.sleep(2.0)
        if esp32_websocket is my_socket and not _welcome_sent_for_session:
            await _maybe_send_welcome_tts()

    asyncio.ensure_future(_welcome_fallback())

    # ── Heartbeat (detecta corte WiFi / apagado en pocos segundos) ─────────
    async def _heartbeat():
        nonlocal esp32_last_pong
        while esp32_websocket is my_socket:
            await asyncio.sleep(ESP32_PING_INTERVAL_S)
            if esp32_websocket is not my_socket:
                break
            now = asyncio.get_event_loop().time()
            if now - esp32_last_pong > ESP32_PONG_TIMEOUT_S:
                logger.warning("⚠️ ESP32 sin PONG — cerrando sesión")
                try:
                    await my_socket.close()
                except Exception:
                    pass
                break
            try:
                await esp32_websocket.send("PING")
            except Exception as e:
                logger.warning(f"⚠️ ESP32 heartbeat falló: {e}")
                try:
                    await my_socket.close()
                except Exception:
                    pass
                break

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
                esp32_last_pong = asyncio.get_event_loop().time()
                continue

            esp32_last_pong = asyncio.get_event_loop().time()
            logger.info(f"[WS] 📨 {message[:100]}")

            # ── HELLO ─────────────────────────────────────────────────────────
            if message.startswith("HELLO:"):
                await websocket.send(f"STATUS:Conectado a {active_room_name}")

            elif message == "PEER_READY":
                logger.info("🔊 PEER_READY (audio Dev opcional)")
                asyncio.ensure_future(_maybe_send_welcome_tts())

            # ── MODE ──────────────────────────────────────────────────────────
            elif message.startswith("MODE:"):
                mode = message.split(":")[1].strip().lower()
                asyncio.ensure_future(
                    _apply_bridge_mode(mode, source="esp32")
                )

            # ── IMG_START ─────────────────────────────────────────────────────
            elif message.startswith("IMG_START:"):
                parts = message.split(":")
                _collecting_record = False
                _record_buffer = bytearray()
                _img_mode  = parts[1] if len(parts) > 2 else "describe"
                _img_total = int(parts[-1])
                _img_buffer = bytearray()
                _collecting_image = True
                await _broadcast_phones(f"EVENT:esp-photo-in:{_img_mode}:{_img_total}")

            # ── IMG_END ───────────────────────────────────────────────────────
            elif message == "IMG_END":
                _collecting_image = False
                received = len(_img_buffer)
                logger.info(f"📷 Imagen: {received}/{_img_total} bytes")
                await _broadcast_phones(
                    f"EVENT:esp-photo-done:{_img_mode}:{received}"
                )

                if received > 0:
                    img_b64 = base64.b64encode(_img_buffer).decode("utf-8")
                    sent_agent = False
                    if active_room is not None:
                        sent_agent = (
                            await safe_publish_data(
                                f"IMG_START:{_img_mode}:0".encode()
                            )
                            and await safe_publish_data(
                                f"IMG_CHUNK:0:{img_b64}".encode()
                            )
                            and await safe_publish_data(b"IMG_END")
                        )
                    if sent_agent:
                        logger.info("✅ Imagen enviada al agente LiveKit")
                    else:
                        logger.info("📷 Imagen → Groq directo (sin LiveKit)")
                        asyncio.ensure_future(
                            _img_reply_groq(img_b64, _img_mode)
                        )
                _img_buffer = bytearray()

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

            elif message == "RECORD_BTN":
                logger.info("[WS] Botón B (assistant) — pedir mic al teléfono")
                await _broadcast_phones("EVENT:btn-b:record")
                await _notify_phone("RECORD_BTN")

            elif message.startswith("BTN:") or message.startswith("BTN_"):
                # ESP32 puede notificar pulsaciones (para feedback visible).
                await _broadcast_phones(f"EVENT:btn:{message.split(':', 1)[-1]}")

            # ── OBSTACLE ──────────────────────────────────────────────────────
            elif message.startswith("OBSTACLE:"):
                dist = message.split(":")[1].strip()
                asyncio.ensure_future(_relay_obstacle(dist, source="esp32"))
                logger.info(f"[WS] OBSTACLE:{dist}")

            else:
                logger.warning(f"[WS] ❓ Desconocido: {message[:80]}")

    except Exception as e:
        logger.error(f"❌ Error en loop principal: {e}", exc_info=True)
    finally:
        heartbeat_task.cancel()
        await _esp32_detach(my_socket, reason="ws cerrado")
        _collecting_image = False
        _collecting_record = False
        _record_buffer = bytearray()


async def _maybe_send_phone_welcome(ws) -> None:
    pid = id(ws)
    client = phone_clients.get(pid)
    if not client or client.get("welcomed"):
        return
    client["welcomed"] = True
    await _send_phone(ws, "STATUS:Conectado a Navi")
    if esp32_websocket is None:
        asyncio.ensure_future(_generate_and_send_audio(WELCOME_MESSAGE))


async def _phone_finish_image():
    """Procesa JPEG recibido desde el panel web."""
    global _phone_img_buffer, _phone_img_mode
    received = len(_phone_img_buffer)
    logger.info(f"📱 Imagen: {received}/{_phone_img_total} bytes modo={_phone_img_mode}")
    if received > 0:
        img_b64 = base64.b64encode(_phone_img_buffer).decode("utf-8")
        sent_agent = False
        if active_room is not None:
            sent_agent = (
                await safe_publish_data(f"IMG_START:{_phone_img_mode}:0".encode())
                and await safe_publish_data(f"IMG_CHUNK:0:{img_b64}".encode())
                and await safe_publish_data(b"IMG_END")
            )
        if sent_agent:
            logger.info("✅ Imagen (teléfono) → agente LiveKit")
        else:
            asyncio.ensure_future(_img_reply_groq(img_b64, _phone_img_mode))
    _phone_img_buffer = bytearray()


async def handle_phone_quart():
    """Cliente web / teléfono — varios paneles a la vez, estado compartido."""
    global _phone_collecting_record, _phone_record_buffer
    global _phone_record_expected
    global _phone_collecting_image, _phone_img_buffer, _phone_img_mode, _phone_img_total

    logger.info("📱 Nueva conexión WebSocket (teléfono / web)")

    lk_ok = await ensure_livekit_connected(required=False)
    if not lk_ok:
        logger.info("📱 Panel web sin LiveKit (modo Groq directo)")

    my_socket = websocket._get_current_object()
    my_id = _phone_register(my_socket, alerts="local")

    if not lk_ok:
        await _send_phone(my_socket, "LIVEKIT:off")

    await _sync_state_to_phone(my_socket)
    asyncio.ensure_future(_maybe_send_phone_welcome(my_socket))

    try:
        while True:
            message = await websocket.receive()

            if isinstance(message, bytes):
                if _phone_collecting_image:
                    _phone_img_buffer.extend(message)
                elif _phone_collecting_record:
                    _phone_record_buffer.extend(message)
                else:
                    logger.warning(f"📱 BIN sin contexto: {len(message)} B")
                continue

            if message == "PONG":
                continue

            logger.info(f"[phone] {message[:100]}")

            if message.startswith("HELLO:"):
                parts = message.split(":")
                if len(parts) >= 3 and parts[2].strip() == "server-alerts":
                    if my_id in phone_clients:
                        phone_clients[my_id]["alerts"] = "server"
                    logger.info("[phone] alertas por TTS del servidor (móvil/ngrok)")
                await _send_phone(my_socket, f"STATUS:Conectado a {active_room_name}")
                await _sync_state_to_phone(my_socket)

            elif message.startswith("MODE:"):
                mode = message.split(":")[1].strip().lower()
                asyncio.ensure_future(
                    _apply_bridge_mode(
                        mode, source="phone", exclude_phone_id=my_id
                    )
                )

            elif message.startswith("IMG_START:"):
                parts = message.split(":")
                _phone_collecting_record = False
                _phone_record_buffer = bytearray()
                _phone_img_mode = parts[1] if len(parts) > 2 else "describe"
                _phone_img_total = int(parts[-1])
                _phone_img_buffer = bytearray()
                _phone_collecting_image = True
                await _send_phone(my_socket, "PHOTO:on")
                logger.info(f"📱 IMG_START {_phone_img_mode} {_phone_img_total} B")

            elif message == "IMG_END":
                _phone_collecting_image = False
                await _send_phone(my_socket, "PHOTO:off")
                asyncio.ensure_future(_phone_finish_image())

            elif message.startswith("RECORD_START:"):
                _phone_record_expected = int(message.split(":")[1].strip())
                _phone_record_buffer = bytearray()
                _phone_collecting_record = True
                await _send_phone(my_socket, "RECORDING:on")
                logger.info(f"📱 RECORD_START {_phone_record_expected} B")

            elif message == "RECORD_END":
                _phone_collecting_record = False
                wav_copy = bytes(_phone_record_buffer)
                _phone_record_buffer = bytearray()
                await _send_phone(my_socket, "RECORDING:off")
                asyncio.ensure_future(
                    _transcribe_wav_and_publish_to_agent(wav_copy, source="phone")
                )

            elif message == "PING":
                await websocket.send("PONG")

            else:
                logger.warning(f"[phone] desconocido: {message[:80]}")

    except Exception as e:
        logger.error(f"❌ phone WS: {e}", exc_info=True)
    finally:
        _phone_unregister(my_socket)
        _phone_collecting_record = False
        _phone_record_buffer = bytearray()
        _phone_collecting_image = False
        _phone_img_buffer = bytearray()