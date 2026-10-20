import asyncio
import json
import random
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Radar canvas is 800x540; planes stay 15px inside the edges.
MIN_X, MAX_X = 15, 785
MIN_Y, MAX_Y = 15, 525
TICK = 0.1
INITIAL_PLANES = 3

planes = {}
clients = set()
next_id = 1
paused = False
landings = 0
state_lock = asyncio.Lock()


def create_Plane(callsign_prefix="FLT"):
    global next_id

    plane_id = next_id
    next_id += 1

    plane = {
        "id": plane_id,
        "callsign": f"{callsign_prefix}-{plane_id:03}",
        "x": 35 + random.random() * 730,
        "y": 35 + random.random() * 470,
        "vx": (random.random() - 0.5) * 45,
        "vy": (random.random() - 0.5) * 45,
        "status": "AIRBORNE",
    }
    planes[plane_id] = plane
    return plane


def state_Message():
    return json.dumps({
        "type": "state",
        "planes": list(planes.values()),
        "paused": paused,
        "landings": landings,
    })


async def broadcast_State():
    async with state_lock:
        message = state_Message()

    disconnected = set()

    for client in list(clients):
        try:
            await client.send_text(message)
        except Exception:
            disconnected.add(client)

    clients.difference_update(disconnected)


async def simulation_Loop():
    while True:
        await asyncio.sleep(TICK)

        async with state_lock:
            if not paused:
                for plane in planes.values():
                    plane["x"] += plane["vx"] * TICK
                    plane["y"] += plane["vy"] * TICK

                    if plane["x"] < MIN_X or plane["x"] > MAX_X:
                        plane["vx"] *= -1
                    if plane["y"] < MIN_Y or plane["y"] > MAX_Y:
                        plane["vy"] *= -1

                    plane["x"] = max(MIN_X, min(MAX_X, plane["x"]))
                    plane["y"] = max(MIN_Y, min(MAX_Y, plane["y"]))

        await broadcast_State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with state_lock:
        for _ in range(INITIAL_PLANES):
            create_Plane()

    task = asyncio.create_task(simulation_Loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def tower():
    return FileResponse(STATIC_DIR / "ControllerIndex.html")


async def handle_Messages(websocket: WebSocket, plane_id=None):
    global paused

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

        if not isinstance(signal, dict):
            continue

        async with state_lock:
            if signal.get("type") == "SPAWN_PLANE":
                create_Plane()
            elif signal.get("type") == "TOGGLE_PAUSE":
                paused = not paused
            elif signal.get("type") == "LAND_REQUEST" and plane_id in planes:
                planes[plane_id]["status"] = "LAND_REQUESTED"

        # The simulation loop broadcasts every tick, so the change reaches
        # every connected radar within TICK seconds.


@app.websocket("/ws/tower")
async def tower_websocket(websocket: WebSocket):
    """Radar screens: watch the shared airspace without adding a plane."""
    await websocket.accept()

    async with state_lock:
        snapshot = state_Message()
    await websocket.send_text(snapshot)
    clients.add(websocket)

    try:
        await handle_Messages(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Pilot devices: each connection flies its own plane."""
    await websocket.accept()

    async with state_lock:
        plane_id = create_Plane("SFS")["id"]
        snapshot = state_Message()
    await websocket.send_text(snapshot)
    clients.add(websocket)

    try:
        await handle_Messages(websocket, plane_id)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)

        async with state_lock:
            planes.pop(plane_id, None)
