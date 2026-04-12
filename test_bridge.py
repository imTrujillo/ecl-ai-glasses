"""
tst_bridge.py — Cliente de prueba WebSocket para el bridge de Navi
Protocolo nuevo: AUDIO_STREAM → <bytes binarios> → AUDIO_END
Guarda los chunks como WAV reproducible (8 kHz, mono, u8).
"""
import asyncio
import struct
import websockets


WS_URI = "ws://localhost:8000/ws"
# WS_URI = "wss://ecl-ai-glasses-production.up.railway.app/ws"

SAMPLE_RATE = 8000
CHANNELS    = 1
SAMPLE_WIDTH = 1   # u8 = 1 byte por muestra


def make_wav_header(num_samples: int) -> bytes:
    """Genera una cabecera WAV para PCM u8 8kHz mono."""
    data_size   = num_samples * CHANNELS * SAMPLE_WIDTH
    chunk_size  = 36 + data_size
    byte_rate   = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
    block_align = CHANNELS * SAMPLE_WIDTH
    bits        = SAMPLE_WIDTH * 8

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        chunk_size,
        b"WAVE",
        b"fmt ",
        16,            # PCM chunk size
        1,             # formato PCM
        CHANNELS,
        SAMPLE_RATE,
        byte_rate,
        block_align,
        bits,
        b"data",
        data_size,
    )
    return header


async def test():
    print(f"🔌 Conectando a {WS_URI}...")
    async with websockets.connect(
        WS_URI,
        ping_interval=20,
        ping_timeout=10,
        max_size=2**20,   # 1 MB por mensaje
    ) as ws:
        print("✅ Conectado al bridge\n")

        audio_count  = 0
        pcm_buffer   = bytearray()
        streaming    = False

        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=60)

                # ── Bytes binarios — chunk PCM RAW u8 ──────────────────────────
                if isinstance(msg, bytes):
                    if streaming:
                        pcm_buffer.extend(msg)
                        print(f"  📦 +{len(msg):>5} bytes  (total: {len(pcm_buffer):>7})")
                    else:
                        print(f"  ⚠️  Bytes fuera de contexto ({len(msg)} bytes) — ignorados")

                # ── Mensajes de texto ───────────────────────────────────────────
                elif isinstance(msg, str):

                    # Nuevo protocolo — inicio de stream (sin tamaño)
                    if msg == "AUDIO_STREAM":
                        pcm_buffer = bytearray()
                        streaming  = True
                        print(f"\n🔊 AUDIO_STREAM recibido — acumulando PCM RAW u8 8kHz...")

                    # Fin del stream
                    elif msg == "AUDIO_END":
                        streaming = False
                        audio_count += 1
                        filename = f"audio_{audio_count}.wav"

                        # Construir WAV con cabecera correcta
                        header = make_wav_header(len(pcm_buffer))
                        with open(filename, "wb") as f:
                            f.write(header)
                            f.write(pcm_buffer)

                        duration = len(pcm_buffer) / SAMPLE_RATE
                        print(f"✅ Guardado: {filename}  "
                              f"({len(pcm_buffer)} bytes PCM | {duration:.1f}s)\n")
                        pcm_buffer = bytearray()

                        # Simular ESP32: responder y sincronizar modo
                        await ws.send("HELLO:esp32-test")

                    # STATUS — servidor listo
                    elif msg.startswith("STATUS:"):
                        print(f"📨 {msg}")
                        await ws.send("MODE:assistant")
                        print("✅ Modo sincronizado — esperando audio de bienvenida...\n")

                    elif msg == "PING":
                        await ws.send("PONG")
                        print("💓 PING → PONG")

                    elif msg.startswith("ERROR:"):
                        print(f"🔴 {msg}")

                    else:
                        print(f"📨 {msg}")

            except asyncio.TimeoutError:
                print("⏱️  Timeout — sin mensajes en 60s")
                break
            except websockets.ConnectionClosed as e:
                print(f"🔌 Conexión cerrada: {e}")
                break


asyncio.run(test())