"""
Funciones callable que el LLM puede invocar automáticamente.
Compatible con livekit-agents >= 1.0 (usa @function_tool).
"""
import logging
from datetime import datetime
from typing import Annotated

import httpx
from livekit.agents import function_tool, RunContext

logger = logging.getLogger("assistant-functions")
logger.setLevel(logging.INFO)


# En v1.x las tools son funciones sueltas decoradas con @function_tool
# y se pasan como lista al Agent(tools=[...])

@function_tool
async def get_current_time(
    dummy: Annotated[str, "Ignorar, no se usa"] = "",
) -> str:
    """Retorna la hora y fecha actual."""
    now = datetime.now()
    return (
        f"Son las {now.strftime('%I:%M %p')} "
        f"del {now.strftime('%A %d de %B de %Y')}"
    )


@function_tool
async def get_weather(
    city: Annotated[str, "Nombre de la ciudad, ejemplo: San Salvador"] = "San Salvador",
) -> str:
    """
    Obtiene el clima actual de cualquier ciudad.
    Usa Open-Meteo + Nominatim — 100% gratuito, sin API key.
    """
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            # 1. Geocodificar ciudad → lat/lon
            geo = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": city, "format": "json", "limit": 1},
                headers={"User-Agent": "SmartGlassesAgent/1.0"},
            )
            geo.raise_for_status()
            results = geo.json()
            if not results:
                return f"No encontré la ciudad: {city}"

            lat = results[0]["lat"]
            lon = results[0]["lon"]

            # 2. Consultar clima
            weather = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current_weather": True,
                    "hourly": "precipitation_probability",
                    "forecast_days": 1,
                },
            )
            weather.raise_for_status()
            data = weather.json()

        current = data["current_weather"]
        temp = current["temperature"]
        wind = current["windspeed"]
        code = current["weathercode"]
        desc = _wmo_description(code)
        rain_prob = data.get("hourly", {}).get("precipitation_probability", [0])[0]

        return (
            f"En {city}: {desc}, {temp}°C, "
            f"viento {wind} km/h, "
            f"probabilidad de lluvia {rain_prob}%."
        )

    except Exception as e:
        logger.error(f"Weather error: {e}")
        return "No pude obtener el clima en este momento."


@function_tool
async def set_reminder(
    message: Annotated[str, "Qué debe recordar el usuario"] = "Tu recordatorio",
    minutes: Annotated[int, "En cuántos minutos avisar"] = 5,
) -> str:
    """Establece un recordatorio verbal para el usuario."""
    return f"Listo, te recordaré: '{message}' en {minutes} minutos."


# Lista exportable para pasarle al Agent
def all_tools():
    return [get_current_time, get_weather, set_reminder]


# ── Utilidad: códigos WMO → descripción ──────────────────────────────────────
def _wmo_description(code: int) -> str:
    descriptions = {
        0: "cielo despejado",
        1: "mayormente despejado",
        2: "parcialmente nublado",
        3: "nublado",
        45: "niebla",
        51: "llovizna ligera",
        61: "lluvia leve",
        63: "lluvia moderada",
        65: "lluvia intensa",
        80: "chubascos",
        95: "tormenta",
    }
    for threshold in sorted(descriptions.keys(), reverse=True):
        if code >= threshold:
            return descriptions[threshold]
    return "condición desconocida"