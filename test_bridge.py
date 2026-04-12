import asyncio
import websockets

async def test():
    uri = "ws://localhost:8000/ws"
    # uri = "wss://ecl-ai-glasses-production.up.railway.app/ws"
    # uri = "wss://lexicological-semestrial-johnathan.ngrok-free.dev/ws"
    
    async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
        print("✅ Conectado al bridge")
        audio_count = 0
        mp3_size = 0
        mp3_buffer = bytearray()

        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=30)

                if isinstance(msg, bytes):
                    # ✅ Bytes binarios del MP3
                    mp3_buffer.extend(msg)
                    print(f"  📦 {len(msg)} bytes recibidos (total: {len(mp3_buffer)}/{mp3_size})")

                elif isinstance(msg, str):
                    if msg.startswith("AUDIO_START:"):
                        mp3_size = int(msg.split(":")[1])
                        mp3_buffer = bytearray()
                        print(f"\n🔊 Audio WAV iniciando — {mp3_size} bytes esperados")

                    elif msg == "AUDIO_END":
                        audio_count += 1
                        filename = f"audio_{audio_count}.wav"
                        with open(filename, "wb") as f:
                            f.write(mp3_buffer)
                        print(f"✅ Guardado: {filename} ({len(mp3_buffer)} bytes)")
                        mp3_buffer = bytearray()
                        await ws.send("HELLO:esp32-test")
                    
                    elif msg.startswith("STATUS:"):
                        print(f"📨 {msg}")
                        await ws.send("MODE:assistant")
                        print("✅ Listo — habla en el playground...")
                    
                    else:
                        print(f"📨 {msg}")

            except asyncio.TimeoutError:
                print("⏱️ Timeout")
                break

asyncio.run(test())