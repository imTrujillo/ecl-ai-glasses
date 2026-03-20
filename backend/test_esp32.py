import asyncio
import base64
import httpx
from livekit import rtc
from dotenv import load_dotenv
import os

load_dotenv()

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
SERVER_URL = "http://localhost:8000"

async def get_token(room: str = None) -> tuple[str, str]:
    """Obtiene token y nombre de sala desde el servidor local."""
    async with httpx.AsyncClient() as client:
        params = {"name": "esp32-test"}
        if room:
            params["room"] = room
        response = await client.get(f"{SERVER_URL}/getToken", params=params)
        data = response.json()
        return data["token"], data["room"]

async def main():
    # Pide sala opcionalmente
    room_input = input("Nombre de sala (Enter para generar nueva): ").strip()
    room = room_input if room_input else None
    
    print("Obteniendo token del servidor...")
    token, room_name = await get_token(room)
    print(f"✅ Token obtenido para sala: {room_name}")

    room_obj = rtc.Room()
    await room_obj.connect(LIVEKIT_URL, token)
    print("Conectado a LiveKit")

    while True:
        print("\n¿Qué quieres simular?")
        print("1 → Modo asistente de voz")
        print("2 → Modo describe entorno")
        print("3 → Modo OCR")
        print("4 → Enviar imagen de prueba")
        print("q → Salir")

        opcion = input(">>> ").strip()

        if opcion == "1":
            await room_obj.local_participant.publish_data("MODE:assistant".encode())
            print("✅ Enviado MODE:assistant")
        elif opcion == "2":
            await room_obj.local_participant.publish_data("MODE:describe".encode())
            print("✅ Enviado MODE:describe")
        elif opcion == "3":
            await room_obj.local_participant.publish_data("MODE:ocr".encode())
            print("✅ Enviado MODE:ocr")
        elif opcion == "4":
            image_path = input("Ruta de la imagen: ").strip()
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            chunks = [b64[i:i+10000] for i in range(0, len(b64), 10000)]
            print(f"Enviando imagen en {len(chunks)} partes...")
            await room_obj.local_participant.publish_data(f"IMG_START:{len(chunks)}".encode())
            await asyncio.sleep(0.1)
            for i, chunk in enumerate(chunks):
                await room_obj.local_participant.publish_data(f"IMG_CHUNK:{i}:{chunk}".encode())
                await asyncio.sleep(0.05)
            await room_obj.local_participant.publish_data("IMG_END".encode())
            print("✅ Imagen enviada completa")
        elif opcion == "q":
            break

    await room_obj.disconnect()

asyncio.run(main())