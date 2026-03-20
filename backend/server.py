"""
Servidor de tokens LiveKit.
Usa Quart (async Flask) para evitar problemas con coroutines.
"""
import os
import uuid

from quart import Quart, request, jsonify
from quart_cors import cors
from livekit import api
from livekit.api import LiveKitAPI, ListRoomsRequest
from dotenv import load_dotenv

load_dotenv()

app = Quart(__name__)
app = cors(app, allow_origin="*")


async def get_rooms() -> list[str]:
    lk = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    rooms = await lk.room.list_rooms(ListRoomsRequest())
    await lk.aclose()
    return [room.name for room in rooms.rooms]


async def generate_room_name() -> str:
    existing = await get_rooms()
    while True:
        name = "room-" + str(uuid.uuid4())[:8]
        if name not in existing:
            return name


@app.route("/getToken")
async def get_token():
    name = request.args.get("name", "guest")
    room = request.args.get("room", None)

    if not room:
        room = await generate_room_name()

    # ✅ AccessToken NO es async — no uses await aquí
    token = (
        api.AccessToken(
            os.getenv("LIVEKIT_API_KEY"),
            os.getenv("LIVEKIT_API_SECRET"),
        )
        .with_identity(name)
        .with_name(name)
        .with_grants(api.VideoGrants(room_join=True, room=room))
    )

    return jsonify({"token": token.to_jwt(), "room": room})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)