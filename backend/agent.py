from __future__ import annotations

import asyncio
import logging
import os

import httpx
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    cli,
    Agent,
    AgentSession,
)
from livekit.plugins import groq, elevenlabs, silero
from dotenv import load_dotenv
from api import all_tools
from prompts import INSTRUCTIONS, WELCOME_MESSAGE, MODE_OCR, MODE_DESCRIBE, MODE_ASSISTANT

load_dotenv()

logger = logging.getLogger("smart-glasses-agent")
logger.setLevel(logging.INFO)


async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    participant = await ctx.wait_for_participant()
    logger.info(f"Participant connected: {participant.identity}")

    current_mode = {"value": "assistant"}
    image_chunks = {"parts": [], "total": 0}

    mode_prompts = {
        "ocr": MODE_OCR,
        "describe": MODE_DESCRIBE,
        "assistant": MODE_ASSISTANT,
    }

    agent = Agent(
        instructions=INSTRUCTIONS,
        tools=all_tools(),
    )

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=groq.STT(model="whisper-large-v3", language="es"),
        llm=groq.LLM(model="llama-3.3-70b-versatile"),
        tts=elevenlabs.TTS(
            voice_id="XrExE9yKIg1WjnnlVkGX",
            api_key=os.getenv("ELEVEN_API_KEY"),
            streaming_latency=0,
            auto_mode=False,
        ),
    )

    # ── 1. Definir función de visión PRIMERO ──────────────────────────────────
    async def _process_image(image_b64: str):
        mode = current_mode["value"]
        instruction = mode_prompts.get(mode, MODE_DESCRIBE)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}"},
                    json={
                        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": instruction},
                                {"type": "image_url", "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_b64}"
                                }}
                            ]
                        }],
                        "max_tokens": 300
                    }
                )
            result = response.json()
            logger.info(f"Groq vision response: {result}")
            description = result["choices"][0]["message"]["content"]
            logger.info(f"Vision OK — mode={mode}")
            session.say(description)
        except Exception as e:
            logger.error(f"Vision error: {e}")
            session.say("No pude procesar la imagen.")

    # ── 2. Iniciar sesión ─────────────────────────────────────────────────────
    await session.start(room=ctx.room, agent=agent)
    await session.say(WELCOME_MESSAGE)

    # ── 3. Registrar listener DESPUÉS de que sesión esté corriendo ────────────
    @ctx.room.on("data_received")
    def on_data_received(packet, *args, **kwargs):
        try:
            message = packet.data.decode("utf-8")

            if message.startswith("MODE:"):
                mode = message.split(":")[1].strip().lower()
                if mode in mode_prompts:
                    current_mode["value"] = mode
                    logger.info(f"Mode changed to: {mode}")
                    # Solo confirma si NO es assistant, para no interrumpir la escucha
                    if mode != "assistant":
                        session.say(f"Modo {mode} activado.")

            elif message.startswith("IMG_START:"):
                total = int(message.split(":")[1])
                image_chunks["parts"] = []
                image_chunks["total"] = total
                logger.info(f"Recibiendo imagen en {total} partes...")

            elif message.startswith("IMG_CHUNK:"):
                _, idx, data = message.split(":", 2)
                image_chunks["parts"].append((int(idx), data))

            elif message == "IMG_END":
                image_chunks["parts"].sort(key=lambda x: x[0])
                full_b64 = "".join(part for _, part in image_chunks["parts"])
                logger.info(f"Imagen completa — procesando...")
                loop = asyncio.get_event_loop()
                loop.create_task(_process_image(full_b64))

        except Exception as e:
            logger.error(f"DataChannel error: {e}")


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))