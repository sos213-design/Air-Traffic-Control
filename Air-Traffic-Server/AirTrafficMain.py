import asyncio
import io
import json
import math
import platform
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

# Flight model
PX_PER_NM = 10            # radar pixels per nautical mile
CLIMB_STEP = 2000         # feet gained per "Request Higher Altitude"
CLIMB_RATE = 500          # feet per second
MAX_ALTITUDE = 40000
HOLD_RADIUS = 30          # px
APPROACH_SPEED = 30       # px per second

# Conflict detection: two planes are in conflict when they are, or will be
# within LOOKAHEAD seconds on their current tracks, closer than
# SEPARATION_PX horizontally while less than VERTICAL_SEPARATION apart.
SEPARATION_PX = 5 * PX_PER_NM
VERTICAL_SEPARATION = 1000
LOOKAHEAD = 5
RESOLUTION_COOLDOWN = 6   # seconds before the same plane gets another instruction
CONFLICT_DISPLAY = 4      # seconds a conflict stays highlighted after it was last seen

# Destination code (as sent by the cockpit) -> airport. Each has one runway.
airports = {
    "TOWER": {"code": "TOWER", "name": "Main Airport", "runway": "RWY 09", "x": 400, "y": 270},
    "NORTH": {"code": "NORTH", "name": "Northern Airport", "runway": "RWY 18", "x": 400, "y": 70},
    "SOUTH": {"code": "SOUTH", "name": "Southern Airport", "runway": "RWY 36", "x": 400, "y": 470},
}
for airport in airports.values():
    airport["occupant"] = None   # plane id cleared to land
    airport["queue"] = []        # plane ids waiting for clearance, in order

# Server-flown ("AI") planes repeat one of these signal patterns, waiting
# AI_DELAY seconds between steps. Each HOLD is later followed by a second
# HOLD, which leaves the holding pattern. After landing they wait on the
# ground for AI_GROUND_TIME, then take off for another airport and repeat.
AI_PATTERNS = [
    ["ROUTE_CHANGE", "ALT_UP", "HOLD", "HOLD", "LAND_REQUEST"],
    ["ALT_UP", "ROUTE_CHANGE", "ROUTE_CHANGE", "LAND_REQUEST"],
    ["HOLD", "ALT_UP", "HOLD", "ROUTE_CHANGE", "LAND_REQUEST"],
]
AI_DELAY = (6, 12)
AI_GROUND_TIME = 5

planes = {}
ai_planes = {}    # plane id -> {"pattern", "step", "next_at"}
departures = []   # AI planes on the ground: {"at", "callsign", "airport", "pattern"}
radio = []        # tower radio calls for the radars, sent with the next state
sim_time = 0.0    # seconds of simulation; stands still while paused
conflicts = {}            # frozenset({plane id, plane id}) -> sim_time last detected
conflict_total = 0
resolved_until = {}       # plane id -> sim_time of its last instruction + cooldown
holds = {}        # plane id -> {"cx", "cy", "angle"} while circling
pilots = {}       # plane id -> cockpit websocket
clients = set()   # radar websockets
send_locks = {}
next_id = 1
paused = False
landings = 0
state_lock = asyncio.Lock()


def heading_Of(vx, vy):
    """Compass heading in degrees: 0 is up (north), 90 is right (east)."""
    return round(math.degrees(math.atan2(vx, -vy))) % 360


def set_Heading(plane, heading, speed=None):
    speed = speed if speed is not None else math.hypot(plane["vx"], plane["vy"])
    plane["vx"] = math.sin(math.radians(heading)) * speed
    plane["vy"] = -math.cos(math.radians(heading)) * speed
    plane["heading"] = heading % 360


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
        "vx": 0,
        "vy": 0,
        "heading": 0,
        "altitude": random.randint(6, 15) * 1000,
        "target_altitude": 0,
        "status": "AIRBORNE",
        "emergency": False,
        "conflict": False,
    }
    plane["target_altitude"] = plane["altitude"]
    set_Heading(plane, random.randint(0, 359), 15 + random.random() * 15)
    planes[plane_id] = plane
    return plane


def leave_Sequence(plane):
    """Drop a plane's landing clearance or place in line, if it has one."""
    airport = airports[plane["destination"]]
    if airport["occupant"] == plane["id"]:
        airport["occupant"] = None
    if plane["id"] in airport["queue"]:
        airport["queue"].remove(plane["id"])


def remove_Plane(plane_id):
    if plane_id in planes:
        leave_Sequence(planes.pop(plane_id))
    holds.pop(plane_id, None)
    resolved_until.pop(plane_id, None)


def callsign_Of(plane_id):
    return planes[plane_id]["callsign"] if plane_id in planes else None


def state_Message(radio_calls=()):
    return json.dumps({
        "type": "state",
        "planes": list(planes.values()),
        "radio": list(radio_calls),
        "airports": [
            {
                "code": a["code"],
                "name": a["name"],
                "runway": a["runway"],
                "x": a["x"],
                "y": a["y"],
                "occupant": callsign_Of(a["occupant"]),
                "queue": [callsign_Of(pid) for pid in a["queue"]],
            }
            for a in airports.values()
        ],
        "conflicts": [
            sorted(callsign_Of(pid) for pid in pair)
            for pair in conflicts
            if all(pid in planes for pid in pair)
        ],
        "conflict_total": conflict_total,
        "paused": paused,
        "landings": landings,
    })


def flight_Message(plane_id):
    plane = planes[plane_id]
    queue = airports[plane["destination"]]["queue"]
    return {
        "type": "flight",
        "plane": plane,
        "queue_position": queue.index(plane_id) + 1 if plane_id in queue else None,
    }


async def send_Text(websocket, text):
    # Radar broadcasts and replies can overlap; send one message at a time.
    lock = send_locks.setdefault(websocket, asyncio.Lock())
    async with lock:
        await websocket.send_text(text)


async def broadcast_State():
    async with state_lock:
        message = state_Message(radio)
        radio.clear()

    disconnected = set()

    for client in list(clients):
        try:
            await send_Text(client, message)
        except Exception:
            disconnected.add(client)

    clients.difference_update(disconnected)


async def send_To_Pilots(messages):
    for plane_id, message in messages:
        websocket = pilots.get(plane_id)
        if websocket is None:
            continue
        try:
            await send_Text(websocket, json.dumps(message))
        except Exception:
            pass  # the cockpit's own handler cleans up on disconnect


def start_Hold(plane):
    """Circle to the right of the current track, starting where the plane is."""
    speed = max(math.hypot(plane["vx"], plane["vy"]), 15)
    right = math.radians(plane["heading"] + 90)
    cx = plane["x"] + math.sin(right) * HOLD_RADIUS
    cy = plane["y"] - math.cos(right) * HOLD_RADIUS
    cx = max(MIN_X + HOLD_RADIUS, min(MAX_X - HOLD_RADIUS, cx))
    cy = max(MIN_Y + HOLD_RADIUS, min(MAX_Y - HOLD_RADIUS, cy))
    holds[plane["id"]] = {
        "cx": cx,
        "cy": cy,
        "angle": math.atan2(plane["y"] - cy, plane["x"] - cx),
        "speed": speed,
    }


def leave_Hold(plane):
    holds.pop(plane["id"], None)
    plane["status"] = "AIRBORNE"


def clear_To_Land(plane_id, airport):
    plane = planes[plane_id]
    airport["occupant"] = plane_id
    holds.pop(plane_id, None)
    plane["status"] = "APPROACH"
    return (
        f"{plane['callsign']}, cleared to land {airport['runway']} "
        f"at {airport['name']}."
    )


def enqueue(plane_id, airport):
    """Emergencies go ahead of everyone except earlier emergencies."""
    queue = airport["queue"]
    if plane_id in queue:
        queue.remove(plane_id)

    position = len(queue)
    if planes[plane_id]["emergency"]:
        position = sum(1 for pid in queue if planes[pid]["emergency"])
    queue.insert(position, plane_id)
    return position + 1


def step_Plane(plane, events):
    """Advance one plane by one tick. Returns True if it landed."""
    global landings

    plane_id = plane["id"]

    if plane["status"] == "APPROACH":
        airport = airports[plane["destination"]]
        dx, dy = airport["x"] - plane["x"], airport["y"] - plane["y"]
        distance = math.hypot(dx, dy)

        if distance <= APPROACH_SPEED * TICK:
            landings += 1
            events.append((plane_id, {
                "type": "landed",
                "message": (
                    f"{plane['callsign']}, welcome to {airport['name']}. "
                    "Taxi to the gate."
                ),
            }))
            return True

        set_Heading(plane, heading_Of(dx, dy), APPROACH_SPEED)
        plane["x"] += plane["vx"] * TICK
        plane["y"] += plane["vy"] * TICK
        # Descend evenly so the plane reaches the ground at the runway.
        plane["altitude"] *= (distance - APPROACH_SPEED * TICK) / distance
        plane["target_altitude"] = plane["altitude"]
        return False

    if plane_id in holds:
        hold = holds[plane_id]
        hold["angle"] += hold["speed"] / HOLD_RADIUS * TICK
        plane["x"] = hold["cx"] + math.cos(hold["angle"]) * HOLD_RADIUS
        plane["y"] = hold["cy"] + math.sin(hold["angle"]) * HOLD_RADIUS
        # Velocity along the circle, so radars can extrapolate smoothly.
        plane["vx"] = -math.sin(hold["angle"]) * hold["speed"]
        plane["vy"] = math.cos(hold["angle"]) * hold["speed"]
        plane["heading"] = heading_Of(plane["vx"], plane["vy"])
    else:
        plane["x"] += plane["vx"] * TICK
        plane["y"] += plane["vy"] * TICK

        if plane["x"] < MIN_X or plane["x"] > MAX_X:
            plane["vx"] *= -1
        if plane["y"] < MIN_Y or plane["y"] > MAX_Y:
            plane["vy"] *= -1

        plane["x"] = max(MIN_X, min(MAX_X, plane["x"]))
        plane["y"] = max(MIN_Y, min(MAX_Y, plane["y"]))
        plane["heading"] = heading_Of(plane["vx"], plane["vy"])

    if plane["altitude"] != plane["target_altitude"]:
        if plane["altitude"] < plane["target_altitude"]:
            plane["altitude"] = min(
                plane["target_altitude"],
                plane["altitude"] + CLIMB_RATE * TICK,
            )
        else:
            plane["altitude"] = max(
                plane["target_altitude"],
                plane["altitude"] - CLIMB_RATE * TICK,
            )
        if plane["altitude"] == plane["target_altitude"]:
            events.append((plane_id, {
                "type": "ack",
                "message": (
                    f"{plane['callsign']}, reaching {plane['altitude']:,.0f} feet, "
                    "maintain."
                ),
            }))
    return False


def predicted_Position(plane, t):
    """Where the plane will be (x, y, altitude) in t seconds."""
    if plane["status"] == "APPROACH":
        airport = airports[plane["destination"]]
        distance = math.hypot(airport["x"] - plane["x"], airport["y"] - plane["y"])
        progress = min(1, APPROACH_SPEED * t / distance) if distance else 1
        return (
            plane["x"] + (airport["x"] - plane["x"]) * progress,
            plane["y"] + (airport["y"] - plane["y"]) * progress,
            plane["altitude"] * (1 - progress),
        )

    change = plane["target_altitude"] - plane["altitude"]
    altitude = plane["altitude"] + max(-CLIMB_RATE * t, min(CLIMB_RATE * t, change))
    if plane["id"] in holds:
        # Circling planes stay roughly where they are.
        return plane["x"], plane["y"], altitude
    return plane["x"] + plane["vx"] * t, plane["y"] + plane["vy"] * t, altitude


def in_Conflict(a, b):
    """True if the planes break separation now or within LOOKAHEAD seconds."""
    steps = int(LOOKAHEAD / 0.5)
    for i in range(steps + 1):
        ax, ay, a_alt = predicted_Position(a, i * 0.5)
        bx, by, b_alt = predicted_Position(b, i * 0.5)
        if (abs(a_alt - b_alt) < VERTICAL_SEPARATION
                and math.hypot(ax - bx, ay - by) < SEPARATION_PX):
            return True
    return False


def detect_Conflicts():
    found = set()
    ids = list(planes)
    for i, a_id in enumerate(ids):
        for b_id in ids[i + 1:]:
            if in_Conflict(planes[a_id], planes[b_id]):
                found.add(frozenset((a_id, b_id)))
    return found


def resolve_Conflict(plane, other):
    """Course change that moves `plane` away from `other`, or None."""
    # Landing traffic keeps its approach; the other plane gives way.
    if plane["status"] == "APPROACH":
        return None

    miles = math.hypot(plane["x"] - other["x"], plane["y"] - other["y"]) / PX_PER_NM
    alert = f"{plane['callsign']}, traffic alert, {other['callsign']} {miles:.0f} miles"

    # A circling plane breaks off its hold to turn away, and goes back to
    # holding once it is clear (see resume_Holds).
    holding = plane["id"] in holds
    holds.pop(plane["id"], None)

    # Turn up to 90 degrees toward the bearing that points away from the
    # other plane. Head-on, both turn right.
    away = heading_Of(plane["x"] - other["x"], plane["y"] - other["y"])
    turn = (away - plane["heading"] + 540) % 360 - 180
    if abs(turn) >= 170:
        turn = 90
    turn = max(-90, min(90, turn))
    if abs(turn) < 30:
        turn = 30 if turn >= 0 else -30

    new_heading = round((plane["heading"] + turn) / 5) * 5 % 360
    set_Heading(plane, new_heading)
    direction = "right" if turn > 0 else "left"
    message = f"{alert}. Turn {direction} heading {new_heading:03d}"

    # Landing traffic only descends, so a plane above it also climbs away.
    if other["status"] == "APPROACH" and plane["altitude"] >= other["altitude"]:
        target = round(plane["altitude"] + 2 * VERTICAL_SEPARATION, -3)
        if plane["target_altitude"] < target <= MAX_ALTITUDE:
            plane["target_altitude"] = target
            message += f" and climb to {target:,} feet"

    message += " immediately"
    if holding:
        message += ", then re-enter the hold when clear"
    return message + "."


def resume_Holds():
    """Planes turned out of a hold start circling again once clear."""
    for plane in planes.values():
        if (plane["status"] in ("HOLDING", "AWAITING_CLEARANCE")
                and plane["id"] not in holds
                and sim_time >= resolved_until.get(plane["id"], 0)):
            start_Hold(plane)


def handle_Conflicts(events):
    global conflicts, conflict_total

    resume_Holds()

    found = detect_Conflicts()
    conflict_total += len(found - conflicts.keys())

    # Keep each conflict on the radar for a moment after the turn resolves
    # it, so controllers and pilots can see what happened.
    conflicts = {
        pair: seen for pair, seen in conflicts.items()
        if sim_time - seen < CONFLICT_DISPLAY and all(pid in planes for pid in pair)
    }
    for pair in found:
        conflicts[pair] = sim_time

    involved = set().union(*conflicts) if conflicts else set()
    for plane in planes.values():
        plane["conflict"] = plane["id"] in involved

    for pair in found:
        a_id, b_id = pair
        for plane_id, other_id in ((a_id, b_id), (b_id, a_id)):
            if sim_time < resolved_until.get(plane_id, 0):
                continue

            message = resolve_Conflict(planes[plane_id], planes[other_id])
            if message is None:
                continue

            resolved_until[plane_id] = sim_time + RESOLUTION_COOLDOWN
            radio.append(message)
            events.append((plane_id, {"type": "traffic_alert", "message": message}))

            # Don't let the AI's own pattern turn it straight back.
            if plane_id in ai_planes:
                ai = ai_planes[plane_id]
                ai["next_at"] = max(ai["next_at"], sim_time + RESOLUTION_COOLDOWN)


async def simulation_Loop():
    global sim_time

    while True:
        await asyncio.sleep(TICK)
        events = []

        async with state_lock:
            if not paused:
                sim_time += TICK

                for plane in list(planes.values()):
                    if step_Plane(plane, events):
                        land_AI(plane)
                        remove_Plane(plane["id"])

                # Clear the next plane in line onto any runway that is free.
                for airport in airports.values():
                    if airport["occupant"] is None and airport["queue"]:
                        plane_id = airport["queue"].pop(0)
                        message = clear_To_Land(plane_id, airport)
                        radio.append(message)
                        events.append((plane_id, {"type": "ack", "message": message}))

                handle_Conflicts(events)
                run_AI()

            updates = [(pid, flight_Message(pid)) for pid in pilots if pid in planes]

        await broadcast_State()
        await send_To_Pilots(events + updates)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with state_lock:
        for i in range(INITIAL_PLANES):
            spawn_AI_Plane(AI_PATTERNS[i % len(AI_PATTERNS)])

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


def lan_Addresses():
    """This computer's IPv4 addresses that a phone might reach, best guess first."""
    found = []

    # Connecting a UDP socket sends nothing; it just picks the interface the
    # computer uses to reach the internet, which is usually the Wi-Fi one.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            found.append(s.getsockname()[0])
    except OSError:
        pass

    # Every other adapter, in case that guess is a VPN or virtual network.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass

    addresses = []
    for address in found:
        if address.startswith(("127.", "169.254.")) or address in addresses:
            continue
        addresses.append(address)
    return addresses


def is_Loopback(host):
    return host in ("localhost", "::1") or host.startswith("127.")


def cockpit_Url(scheme, host, port):
    port = f":{port}" if port else ""
    return f"{scheme}://{host}{port}/cockpit"


def join_Hosts(request: Request):
    """Addresses to offer phones. A phone can't reach "localhost", so when the
    page was opened that way, use this computer's network addresses instead."""
    host = request.url.hostname
    if not is_Loopback(host):
        return [host]
    return lan_Addresses() or [host]


async def accepts_Connections(host, port):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 1)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


def running_In_WSL():
    return "microsoft" in platform.release().lower()


@app.get("/api/join")
async def join_Info(request: Request):
    hosts = join_Hosts(request)
    port = request.url.port or (443 if request.url.scheme == "https" else 80)

    # If the server only listens on 127.0.0.1 (plain `uvicorn AirTrafficMain:app`),
    # connecting to our own network addresses fails, and so will every phone.
    checks = [accepts_Connections(h, port) for h in hosts if not is_Loopback(h)]
    reachable = any(await asyncio.gather(*checks))

    return {
        "urls": [cockpit_Url(request.url.scheme, h, request.url.port) for h in hosts],
        "hosts": hosts,
        "network_reachable": reachable,
        "wsl": running_In_WSL(),
    }


@app.get("/qr.svg")
async def join_Qr(request: Request, host: str = ""):
    hosts = join_Hosts(request)
    if host not in hosts:
        host = hosts[0]

    image = qrcode.make(
        cockpit_Url(request.url.scheme, host, request.url.port),
        image_factory=qrcode.image.svg.SvgPathImage,
        border=2,
    )
    buffer = io.BytesIO()
    image.save(buffer)
    return Response(
        buffer.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


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

        await send_Text(websocket, json.dumps({
            "type": "error",
            "message": "JSON decoding error",
        }))


@app.websocket("/ws/tower")
async def tower_websocket(websocket: WebSocket):
    """Radar screens: watch the shared airspace without adding a plane."""
    global paused

    await websocket.accept()

    async with state_lock:
        snapshot = state_Message()
    await send_Text(websocket, snapshot)
    clients.add(websocket)

    try:
        while True:
            signal = await read_Signal(websocket)

            # The simulation loop broadcasts every tick, so changes reach
            # every connected radar within TICK seconds.
            async with state_lock:
                if signal.get("type") == "SPAWN_PLANE":
                    spawn_AI_Plane()
                elif signal.get("type") == "TOGGLE_PAUSE":
                    paused = not paused
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)
        send_locks.pop(websocket, None)


def request_Landing(plane):
    airport = airports[plane["destination"]]

    if plane["status"] == "APPROACH":
        return f"{plane['callsign']}, you are already cleared to land, continue approach."
    if plane["id"] in airport["queue"]:
        position = airport["queue"].index(plane["id"]) + 1
        return f"{plane['callsign']}, you are number {position} for landing, continue holding."

    if airport["occupant"] is None and not airport["queue"]:
        return clear_To_Land(plane["id"], airport)

    position = enqueue(plane["id"], airport)
    if plane["id"] not in holds:
        start_Hold(plane)
    plane["status"] = "AWAITING_CLEARANCE"
    return (
        f"{plane['callsign']}, {airport['runway']} is occupied. Hold, you are "
        f"number {position} for landing at {airport['name']}."
    )


def request_Climb(plane):
    if plane["status"] == "APPROACH":
        return f"{plane['callsign']}, unable, you are on approach. Maintain glide path."
    if plane["target_altitude"] >= MAX_ALTITUDE:
        return f"{plane['callsign']}, unable, {MAX_ALTITUDE:,} feet is the ceiling."

    plane["target_altitude"] = min(MAX_ALTITUDE, plane["target_altitude"] + CLIMB_STEP)
    return (
        f"{plane['callsign']}, climb and maintain "
        f"{plane['target_altitude']:,.0f} feet."
    )


def request_Route_Change(plane):
    if plane["status"] == "APPROACH":
        return f"{plane['callsign']}, unable, you are cleared to land. Continue approach."
    if plane["status"] == "AWAITING_CLEARANCE":
        return f"{plane['callsign']}, unable, hold until cleared to land."

    if plane["id"] in holds:
        leave_Hold(plane)

    turn = random.choice([-1, 1]) * random.randint(45, 135)
    new_heading = (plane["heading"] + turn) % 360
    set_Heading(plane, new_heading)
    direction = "right" if turn > 0 else "left"
    return f"{plane['callsign']}, turn {direction} heading {new_heading:03d}."


def toggle_Hold(plane):
    callsign = plane["callsign"]
    airport = airports[plane["destination"]]

    if plane["status"] == "HOLDING":
        leave_Hold(plane)
        return f"{callsign}, leave the hold, resume own navigation heading {plane['heading']:03d}."

    if plane["status"] == "AWAITING_CLEARANCE":
        leave_Sequence(plane)
        plane["status"] = "HOLDING"
        return f"{callsign}, landing request cancelled. Continue holding."

    if plane["status"] == "APPROACH":
        leave_Sequence(plane)
        start_Hold(plane)
        plane["status"] = "HOLDING"
        return f"{callsign}, approach cancelled, {airport['runway']} released. Enter the hold."

    start_Hold(plane)
    plane["status"] = "HOLDING"
    return f"{callsign}, enter holding pattern as published."


def toggle_Emergency(plane):
    callsign = plane["callsign"]
    plane["emergency"] = not plane["emergency"]

    if not plane["emergency"]:
        return f"{callsign}, emergency cancelled. Resume normal operations."

    airport = airports[plane["destination"]]
    if plane["id"] in airport["queue"]:
        position = enqueue(plane["id"], airport)
        return (
            f"{callsign}, MAYDAY acknowledged. You have priority, "
            f"now number {position} for landing."
        )
    return (
        f"{callsign}, MAYDAY acknowledged. All traffic stand by. "
        "Request landing when ready, you have priority."
    )


def status_Report(plane):
    airport = airports[plane["destination"]]
    distance = math.hypot(airport["x"] - plane["x"], airport["y"] - plane["y"]) / PX_PER_NM
    bearing = heading_Of(airport["x"] - plane["x"], airport["y"] - plane["y"])
    speed = math.hypot(plane["vx"], plane["vy"]) * 10
    return (
        f"{plane['callsign']}, radar contact. Altitude {plane['altitude']:,.0f} feet, "
        f"heading {plane['heading']:03d}, {speed:.0f} knots. {airport['name']} "
        f"bears {bearing:03d}, {distance:.0f} miles. Status "
        f"{plane['status'].replace('_', ' ').lower()}"
        f"{', emergency declared' if plane['emergency'] else ''}."
    )


# Signal name -> handler(plane) returning the tower's reply.
SIGNAL_HANDLERS = {
    "LAND_REQUEST": request_Landing,
    "ALT_UP": request_Climb,
    "ROUTE_CHANGE": request_Route_Change,
    "HOLD": toggle_Hold,
    "MAYDAY": toggle_Emergency,
    "STATUS_REQUEST": status_Report,
}


def transmit(plane, signal_type):
    """Apply a signal to a plane and put the tower's reply on the radio."""
    message = SIGNAL_HANDLERS[signal_type](plane)
    if signal_type != "STATUS_REQUEST":
        radio.append(message)
    return message


def spawn_AI_Plane(pattern=None):
    plane = create_Plane(destination=random.choice(list(airports)))
    add_AI(plane, pattern or random.choice(AI_PATTERNS))
    return plane


def add_AI(plane, pattern):
    ai_planes[plane["id"]] = {
        "pattern": pattern,
        "step": 0,
        "next_at": sim_time + random.uniform(*AI_DELAY),
    }


def land_AI(plane):
    """Park a landed AI plane on the ground until it is time to take off."""
    ai = ai_planes.pop(plane["id"], None)
    if ai:
        departures.append({
            "at": sim_time + AI_GROUND_TIME,
            "callsign": plane["callsign"],
            "airport": plane["destination"],
            "pattern": ai["pattern"],
        })


def take_Off(departure):
    airport = airports[departure["airport"]]
    callsign = departure["callsign"]
    if any(p["callsign"] == callsign for p in planes.values()):
        callsign = None   # a pilot took the name while we were on the ground

    destination = random.choice([code for code in airports if code != airport["code"]])
    plane = create_Plane(callsign, destination)
    plane["x"], plane["y"] = airport["x"], airport["y"]
    plane["altitude"] = 1000
    plane["target_altitude"] = random.randint(6, 12) * 1000
    add_AI(plane, departure["pattern"])
    radio.append(
        f"{plane['callsign']}, cleared for takeoff from {airport['name']}, "
        f"climb to {plane['target_altitude']:,} feet, proceed to "
        f"{airports[destination]['name']}."
    )


def run_AI():
    for departure in [d for d in departures if d["at"] <= sim_time]:
        departures.remove(departure)
        take_Off(departure)

    for plane_id, ai in list(ai_planes.items()):
        plane = planes.get(plane_id)
        if plane is None:
            del ai_planes[plane_id]
            continue

        # Once in the landing sequence, just fly it until touchdown.
        if plane["status"] in ("APPROACH", "AWAITING_CLEARANCE"):
            continue
        if sim_time < ai["next_at"]:
            continue

        signal_type = ai["pattern"][ai["step"]]
        ai["step"] = (ai["step"] + 1) % len(ai["pattern"])
        ai["next_at"] = sim_time + random.uniform(*AI_DELAY)
        transmit(plane, signal_type)


async def handle_Pilot_Signal(plane_id, signal_type):
    async with state_lock:
        plane = planes.get(plane_id)
        if plane is None:
            return {"type": "error", "message": "Register your flight first."}

        if signal_type not in SIGNAL_HANDLERS:
            return {
                "type": "error",
                "message": f"Tower does not recognise signal {signal_type}.",
            }

        message = transmit(plane, signal_type)
        return {"type": "ack", "signal": signal_type, "message": message}


async def register_Pilot(websocket, plane_id, signal):
    callsign = str(signal.get("callsign", "")).strip().upper()
    destination = str(signal.get("destination", "TOWER")).upper()

    if not CALLSIGN_PATTERN.match(callsign):
        return plane_id, {"type": "error", "message": "Invalid callsign."}
    if destination not in airports:
        return plane_id, {"type": "error", "message": "Unknown destination."}

    async with state_lock:
        taken = any(
            p["callsign"] == callsign and p["id"] != plane_id
            for p in planes.values()
        )
        if taken:
            return plane_id, {"type": "error", "message": f"Callsign {callsign} is already in use."}

        if plane_id in planes:
            plane = planes[plane_id]
            if destination != plane["destination"]:
                # A new destination means a new landing sequence.
                if plane["status"] in ("APPROACH", "AWAITING_CLEARANCE"):
                    leave_Sequence(plane)
                    holds.pop(plane_id, None)
                    plane["status"] = "AIRBORNE"
                plane["destination"] = destination
            plane["callsign"] = callsign
        else:
            pilots.pop(plane_id, None)
            plane_id = create_Plane(callsign, destination)["id"]
            pilots[plane_id] = websocket

    return plane_id, {
        "type": "registered",
        "callsign": callsign,
        "destination": destination,
        "message": f"{callsign}, radar contact. Cleared to {airports[destination]['name']}.",
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
                plane_id, reply = await register_Pilot(websocket, plane_id, signal)
            else:
                reply = await handle_Pilot_Signal(plane_id, str(signal.get("type")))

            await send_Text(websocket, json.dumps(reply))
    except WebSocketDisconnect:
        pass
    finally:
        async with state_lock:
            pilots.pop(plane_id, None)
            remove_Plane(plane_id)
        send_locks.pop(websocket, None)


def print_Join_Instructions(port):
    addresses = lan_Addresses()
    print()
    print("  Air Traffic Control server")
    print(f"  Open on this computer:   http://localhost:{port}")
    for address in addresses:
        print(f"  Pilots join from phone:  http://{address}:{port}/cockpit")

    if addresses:
        try:
            qr = qrcode.QRCode(border=1)
            qr.add_data(f"http://{addresses[0]}:{port}/cockpit")
            qr.print_ascii(invert=True)
        except Exception:
            pass   # some terminals can't draw the QR code; the URLs above still work
    else:
        print("  No network connection found. Connect this computer to Wi-Fi.")

    if running_In_WSL():
        print("  WARNING: running inside WSL, which phones usually can't reach.")
        print("  Run this file with Windows Python instead (see README.md).")
    print("  The phone must be on the same Wi-Fi network as this computer.")
    print("  If the code above won't scan, use the one on the start page.")
    print()


if __name__ == "__main__":
    import uvicorn

    PORT = 8000
    print_Join_Instructions(PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
