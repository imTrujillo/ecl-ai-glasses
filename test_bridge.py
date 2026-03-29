import asyncio
import websockets
import io

async def test():
    uri = "ws://localhost:8000/ws"
    # uri = "wss://ecl-ai-glasses-production.up.railway.app/ws"
    
    async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
        print("✅ Conectado al bridge")
        mp3_buffer = bytearray()
        audio_count = 0
        print("🎧 Esperando audio de bienvenida...")
        
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                
                if isinstance(msg, bytes):
                    mp3_buffer.extend(msg)
                    print(f"  📦 {len(msg)} bytes (total: {len(mp3_buffer)})")
                
                elif isinstance(msg, str):
                    if msg.startswith("AUDIO_START:"):
                        print(f"\n🔊 Audio iniciando — {msg.split(':')[1]} bytes")
                        mp3_buffer = bytearray()
                    
                    elif msg == "AUDIO_END":
                        audio_count += 1
                        filename = f"audio_{audio_count}.mp3"
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
                print("⏱️ Timeout — conexión cerrada")
                break

asyncio.run(test())