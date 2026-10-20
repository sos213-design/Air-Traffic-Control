import asyncio
import io
import json
import random
import re
import socket
from contextlib import asynccontextmanager
from pathlib import Path
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Radar canvas is 800x540; planes stay 15px inside the edges.
MIN_X, MAX_X = 15, 785
MIN_Y, MAX_Y = 15, 525
TICK = 0.1
INITIAL_PLANES = 3

CALLSIGN_PATTERN = re.compile(r"^[A-Z0-9-]{1,16}$")
DESTINATIONS = {"TOWER", "NORTH", "SOUTH"}

# Cockpit signal -> (new plane status, tower reply)
PILOT_SIGNALS = {
    "LAND_REQUEST": ("LAND_REQUESTED", "landing request received, expect approach clearance."),
    "ALT_UP": ("CLIMB_REQUESTED", "climb request received, stand by."),
    "ROUTE_CHANGE": ("ROUTE_CHANGE_REQUESTED", "route change request received, stand by."),
    "HOLD": ("HOLDING", "roger, enter holding pattern."),
    "MAYDAY": ("MAYDAY", "emergency acknowledged, all traffic stand by."),
}

planes = {}
clients = set()
next_id = 1
paused = False
landings = 0
state_lock = asyncio.Lock()


def create_Plane(callsign=None, destination="TOWER"):
    global next_id

    plane_id = next_id
    next_id += 1

    plane = {
        "id": plane_id,
        "callsign": callsign or f"FLT-{plane_id:03}",
        "destination": destination,
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
async def open_Page():
    return FileResponse(STATIC_DIR / "Open_Page.html")


@app.get("/tower")
async def tower():
    return FileResponse(STATIC_DIR / "ControllerIndex.html")


@app.get("/cockpit")
async def cockpit():
    return FileResponse(STATIC_DIR / "CockpitIndex.html")


def lan_Address():
    # Connecting a UDP socket sends nothing; it just picks the outgoing interface.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def cockpit_Url(request: Request):
    host = request.url.hostname
    if host in ("localhost", "127.0.0.1", "::1"):
        # A phone can't reach "localhost", so point the QR code at this
        # machine's network address instead.
        host = lan_Address()
    port = f":{request.url.port}" if request.url.port else ""
    return f"{request.url.scheme}://{host}{port}/cockpit"


@app.get("/api/join")
async def join_Info(request: Request):
    return {"url": cockpit_Url(request)}


@app.get("/qr.svg")
async def join_Qr(request: Request):
    image = qrcode.make(
        cockpit_Url(request),
        image_factory=qrcode.image.svg.SvgPathImage,
        border=2,
    )
    buffer = io.BytesIO()
    image.save(buffer)
    return Response(buffer.getvalue(), media_type="image/svg+xml")


async def read_Signal(websocket: WebSocket):
    """Wait for the next valid JSON object from a client."""
    while True:
        message = await websocket.receive_text()

        try:
            signal = json.loads(message)
        except json.JSONDecodeError:
            signal = None

        if isinstance(signal, dict):
            return signal

        await websocket.send_json({
            "type": "error",
            "message": "JSON decoding error",
        })


@app.websocket("/ws/tower")
async def tower_websocket(websocket: WebSocket):
    """Radar screens: watch the shared airspace without adding a plane."""
    global paused

    await websocket.accept()

    async with state_lock:
        snapshot = state_Message()
    await websocket.send_text(snapshot)
    clients.add(websocket)

    try:
        while True:
            signal = await read_Signal(websocket)

            # The simulation loop broadcasts every tick, so changes reach
            # every connected radar within TICK seconds.
            async with state_lock:
                if signal.get("type") == "SPAWN_PLANE":
                    create_Plane()
                elif signal.get("type") == "TOGGLE_PAUSE":
                    paused = not paused
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)


async def handle_Pilot_Signal(plane_id, signal_type):
    async with state_lock:
        plane = planes.get(plane_id)
        if plane is None:
            return {"type": "error", "message": "Register your flight first."}

        if signal_type == "STATUS_REQUEST":
            return {
                "type": "ack",
                "signal": signal_type,
                "message": (
                    f"{plane['callsign']}, radar contact at "
                    f"({plane['x']:.0f}, {plane['y']:.0f}), status {plane['status']}."
                ),
            }

        if signal_type in PILOT_SIGNALS:
            status, reply = PILOT_SIGNALS[signal_type]
            plane["status"] = status
            return {
                "type": "ack",
                "signal": signal_type,
                "status": status,
                "message": f"{plane['callsign']}, {reply}",
            }

    return {"type": "error", "message": f"Tower does not recognise signal {signal_type}."}


async def register_Pilot(plane_id, signal):
    callsign = str(signal.get("callsign", "")).strip().upper()
    destination = str(signal.get("destination", "TOWER")).upper()

    if not CALLSIGN_PATTERN.match(callsign):
        return plane_id, {"type": "error", "message": "Invalid callsign."}
    if destination not in DESTINATIONS:
        return plane_id, {"type": "error", "message": "Unknown destination."}

    async with state_lock:
        taken = any(
            p["callsign"] == callsign and p["id"] != plane_id
            for p in planes.values()
        )
        if taken:
            return plane_id, {"type": "error", "message": f"Callsign {callsign} is already in use."}

        if plane_id in planes:
            planes[plane_id]["callsign"] = callsign
            planes[plane_id]["destination"] = destination
        else:
            plane_id = create_Plane(callsign, destination)["id"]

    return plane_id, {
        "type": "registered",
        "callsign": callsign,
        "destination": destination,
        "message": f"{callsign}, radar contact. Cleared to {destination}.",
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Pilot cockpits: each registered connection flies its own plane."""
    await websocket.accept()
    plane_id = None

    try:
        while True:
            signal = await read_Signal(websocket)

            if signal.get("type") == "REGISTER":
                plane_id, reply = await register_Pilot(plane_id, signal)
            else:
                reply = await handle_Pilot_Signal(plane_id, str(signal.get("type")))

            await websocket.send_json(reply)
    except WebSocketDisconnect:
        pass
    finally:
        async with state_lock:
            planes.pop(plane_id, None)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
