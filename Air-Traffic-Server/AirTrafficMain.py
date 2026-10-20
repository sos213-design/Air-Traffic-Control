import asyncio
import json
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

planes = {}
clients = set()
next_id = 1
state_lock = asyncio.Lock()


@app.get("/")
async def tower():
    return FileResponse(STATIC_DIR / "ControllerIndex.html")

async def broadcast_State():
    message = json.dumps({
        type: "state",
        "planes": list(planes.values()),
    })

    disconnected = set()

    for client in list(clients):
        try:
            await client.send_text(message)
        except Exception:
            disconnected.add(client)

    clients.difference_update(disconnected)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global next_id

    await websocket.accept()
    clients.add(websocket)

    async with state_lock:
        plane_id = next_id
        next_id += 1

        planes[plane_id] = {
            "id": plane_id,
            "callsign": f"SFS-{plane_id}",
            "x": 50 + (plane_id * 73) % 700,
            "y": 50 + (plane_id * 47) % 440,
            "vx": 20,
            "vy": 15,
            "status": "AIRBORNE",
        }

    await broadcast_State()

    try:
        while True:
            message = await websocket.receive_text()

            try:
                signal = json.loads(message)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "message": "JSON decoding error",
                })
                continue

                await broadcast_State()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)

        async with state_lock:
            planes.pop(plane_id, None)

        await broadcast_State()

async def simulation_Loop():
    while True:
        await asyncio.sleep(0.1)

        async with state_lock:
            for plane in planes.values():
                plane["x"] += plane["vx"] * 0.1
                plane["y"] += plane["vy"] * 0.1

                if plane["x"] < 15 or plane["x"] > 785:
                    plane["vx"] *= -1
                if plane["y"] < 15 or plane["y"] > 525:
                    plane["vy"] *= -1

                plane["x"] = max(15, min(785, plane["x"]))
                plane["y"] = max(15, min(525, plane["y"]))

            await broadcast_State()

@app.on_event("startup")
async def startup():
    asyncio.create_task(simulation_Loop())