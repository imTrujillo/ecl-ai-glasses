import asyncio
import subprocess
import sys
import os

async def run_agent():
    mode = "start" if os.getenv("RAILWAY_ENVIRONMENT") else "dev"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "agent.py", mode,
        stdout=sys.stdout, stderr=sys.stderr
    )
    await proc.wait()

async def run_server():
    port = os.getenv('PORT', '8000')
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "hypercorn",
        "server:app",
        "--bind", f"0.0.0.0:{port}",
        "-c", "hypercorn.toml",
        stdout=sys.stdout, stderr=sys.stderr
    )
    await proc.wait()


async def main():
    tasks = [run_server()]
    use_lk = os.getenv("NAVI_USE_LIVEKIT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    if use_lk:
        tasks.append(run_agent())
    else:
        print("[navi] Agente LiveKit omitido (NAVI_USE_LIVEKIT=0)")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())