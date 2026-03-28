import asyncio
import websockets

async def test():
    uri = "ws://localhost:8000/ws"           # local
    # uri = "wss://ecl-ai-glasses-production.up.railway.app/ws"  # railway
    
    async with websockets.connect(uri) as ws:
        print("✅ Conectado al bridge")
        
        # Identificarse como ESP32
        await ws.send("HELLO:esp32-test")
        resp = await asyncio.wait_for(ws.recv(), timeout=5)
        print(f"Respuesta: {resp}")
        
        # Probar cambio de modo
        await ws.send("MODE:describe")
        print("✅ Modo describe enviado")
        
        await asyncio.sleep(2)
        
        await ws.send("MODE:assistant")
        print("✅ Modo assistant enviado")

asyncio.run(test())