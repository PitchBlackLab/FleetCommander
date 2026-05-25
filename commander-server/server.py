import asyncio
import json
import os
import copy
import math
from aiohttp import web
import websockets

# ─── CONNECTION REGISTRIES ───────────────────────────────────────────────────
CONNECTED_CAPTAINS = {}   # {websocket: callsign}
CONNECTED_MAPS     = set()
CONFIG_FILE        = "formations.json"

# ─── RUNTIME STATE (not persisted) ───────────────────────────────────────────
fleet_vectors   = {"pitch": "NEUTRAL", "yaw": "NEUTRAL", "roll": "NEUTRAL", "vertical": "NEUTRAL"}

# Active formation order state
pending_order = {
    "active":       False,       # True while an order is in flight
    "formation":    None,        # name of target formation
    "targets":      {},          # {callsign: {x,y,z}} — destination positions
    "paths":        {},          # {callsign: {ctrl1,ctrl2,color}} — bezier metadata
    "captain_states": {}         # {callsign: "IDLE"|"ACKNOWLEDGED"|"READY"}
}

# Per-captain runtime state
captain_meta = {}   # {callsign: {nearbyRange, view:{yaw,pitch,zoom}, inPosition}}

# ─── PERSISTED SYSTEM DATA ───────────────────────────────────────────────────
SYSTEM_DATA = {
    "colors": {
        "hud":    "#00f0ff",
        "lead":   "#ff0055",
        "escort": "#00ff66",
        "grid":   "#1a3d54",
        "bg":     "#01060f"
    },
    "activeFormation": "Vanguard-Wedge",
    "formations": {
        "Vanguard-Wedge": {
            "Titan-1":  {"ship": "Javelin",      "slot": "Lead",             "x":   0, "y":   0, "z": 0, "size": "LARGE",  "shape": "WEDGE", "color": "#ff0055"},
            "Hammer-1": {"ship": "Hammerhead",   "slot": "Port-Screen",      "x": -80, "y": -60, "z": 0, "size": "MEDIUM", "shape": "ARROW", "color": "#00ff66"},
            "Hammer-2": {"ship": "Hammerhead",   "slot": "Starboard-Screen", "x":  80, "y": -60, "z": 0, "size": "MEDIUM", "shape": "ARROW", "color": "#00ff66"}
        },
        "Suppression-Line": {
            "Titan-1":  {"ship": "Javelin",      "slot": "Lead",             "x":   0, "y":   0,  "z":  0, "size": "LARGE",  "shape": "WEDGE", "color": "#ff0055"},
            "Hammer-1": {"ship": "Hammerhead",   "slot": "Port-Screen",      "x": -150, "y": 0,  "z": 20, "size": "MEDIUM", "shape": "ARROW", "color": "#00ff66"},
            "Hammer-2": {"ship": "Hammerhead",   "slot": "Starboard-Screen", "x":  150, "y": 0,  "z":-20, "size": "MEDIUM", "shape": "ARROW", "color": "#00ff66"}
        }
    },
    "assignments": {},
    "shipLibrary": {},   # {shipName: {shape, scale, color}} — commander overrides
    "gridConfig":  {
        "squaresX": 4, "squaresY": 4, "squaresZ": 2,
        "squareSizeMeters": 100
    },
    "uiLayout": {}       # {panelId: {x,y,w,h}} — layout mode panel positions
}


# ─── DISK I/O ─────────────────────────────────────────────────────────────────

def load_state_from_disk():
    global SYSTEM_DATA
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded = json.load(f)
            if all(k in loaded for k in ["colors", "activeFormation", "formations", "assignments"]):
                # Merge — preserve new keys added in this version
                for k, v in loaded.items():
                    SYSTEM_DATA[k] = v
                if "shipLibrary" not in SYSTEM_DATA: SYSTEM_DATA["shipLibrary"] = {}
                if "gridConfig"  not in SYSTEM_DATA: SYSTEM_DATA["gridConfig"]  = {"squaresX":4,"squaresY":4,"squaresZ":2,"squareSizeMeters":100}
                if "uiLayout"    not in SYSTEM_DATA: SYSTEM_DATA["uiLayout"]    = {}
                print(f"--> SUCCESS: Loaded state from {CONFIG_FILE}")
                return
        except Exception as e:
            print(f"--> ERROR: Failed reading {CONFIG_FILE}: {e}. Using defaults.")
    SYSTEM_DATA["assignments"] = copy.deepcopy(SYSTEM_DATA["formations"][SYSTEM_DATA["activeFormation"]])
    save_state_to_disk()


def save_state_to_disk():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(SYSTEM_DATA, f, indent=4)
        print(f"--> SUCCESS: Flushed state to {CONFIG_FILE}")
    except Exception as e:
        print(f"--> ERROR: Failed flushing state: {e}")


load_state_from_disk()


# ─── BROADCAST HELPERS ────────────────────────────────────────────────────────

def build_sync_all():
    return {
        "type":           "SYNC_ALL",
        "vectors":        fleet_vectors,
        "formation":      SYSTEM_DATA["activeFormation"],
        "assignments":    SYSTEM_DATA["assignments"],
        "colors":         SYSTEM_DATA["colors"],
        "formationList":  list(SYSTEM_DATA["formations"].keys()),
        "onlineCaptains": list(CONNECTED_CAPTAINS.values()),
        "captainStates":  pending_order["captain_states"],
        "pendingOrder":   _public_pending(),
        "shipLibrary":    SYSTEM_DATA["shipLibrary"],
        "gridConfig":     SYSTEM_DATA["gridConfig"],
        "uiLayout":       SYSTEM_DATA["uiLayout"],
        "captainMeta":    captain_meta
    }


def _public_pending():
    """Serialisable snapshot of pending_order for broadcast."""
    if not pending_order["active"]:
        return {"active": False}
    return {
        "active":    True,
        "formation": pending_order["formation"],
        "targets":   pending_order["targets"],
        "paths":     pending_order["paths"]
    }


async def broadcast_to_all(data_dict):
    msg = json.dumps(data_dict)
    targets = list(CONNECTED_CAPTAINS.keys()) + list(CONNECTED_MAPS)
    if targets:
        await asyncio.gather(*[c.send(msg) for c in targets], return_exceptions=True)


async def broadcast_to_maps(data_dict):
    msg = json.dumps(data_dict)
    if CONNECTED_MAPS:
        await asyncio.gather(*[c.send(msg) for c in CONNECTED_MAPS], return_exceptions=True)


# ─── BEZIER PATH COMPUTATION ──────────────────────────────────────────────────

_AXIS_COLORS = {
    "port":      "#ff4444",   # -X  red
    "starboard": "#44ff88",   # +X  green
    "aft":       "#ffffff",   # -Y  white
    "forward":   "#ffee44",   # +Y  yellow
    "keel":      "#44ffff",   # -Z  cyan
    "deck":      "#ccaaff",   # +Z  lavender
}
_AXIS_ORDER = ["port", "starboard", "aft", "forward", "keel", "deck"]


def _dominant_color(dx, dy, dz):
    magnitudes = {
        "port":      max(-dx, 0),
        "starboard": max( dx, 0),
        "aft":       max(-dy, 0),
        "forward":   max( dy, 0),
        "keel":      max(-dz, 0),
        "deck":      max( dz, 0),
    }
    dominant = max(_AXIS_ORDER, key=lambda k: magnitudes[k])
    return _AXIS_COLORS[dominant]


def _vec3_add(a, b):    return (a[0]+b[0], a[1]+b[1], a[2]+b[2])
def _vec3_scale(v, s):  return (v[0]*s, v[1]*s, v[2]*s)
def _vec3_len(v):       return math.sqrt(v[0]**2 + v[1]**2 + v[2]**2)
def _vec3_norm(v):
    l = _vec3_len(v)
    return (v[0]/l, v[1]/l, v[2]/l) if l > 0 else (0, 0, 0)


def _bezier_point(t, p0, c1, c2, p1):
    """Cubic bezier at parameter t."""
    u = 1 - t
    return (
        u**3*p0[0] + 3*u**2*t*c1[0] + 3*u*t**2*c2[0] + t**3*p1[0],
        u**3*p0[1] + 3*u**2*t*c1[1] + 3*u*t**2*c2[1] + t**3*p1[1],
        u**3*p0[2] + 3*u**2*t*c1[2] + 3*u*t**2*c2[2] + t**3*p1[2],
    )


def _paths_conflict(p0a, c1a, c2a, p1a, p0b, c1b, c2b, p1b, min_dist=10, samples=20):
    """Return True if any sampled point pair on two beziers is closer than min_dist."""
    for i in range(samples + 1):
        t = i / samples
        pa = _bezier_point(t, p0a, c1a, c2a, p1a)
        for j in range(samples + 1):
            s = j / samples
            pb = _bezier_point(s, p0b, c1b, c2b, p1b)
            d = _vec3_len((pa[0]-pb[0], pa[1]-pb[1], pa[2]-pb[2]))
            if d < min_dist:
                return True
    return False


def _point_near_targets(point, targets, exclude_callsign, min_dist=10):
    for cs, t in targets.items():
        if cs == exclude_callsign:
            continue
        d = _vec3_len((point[0]-t["x"], point[1]-t["y"], point[2]-t["z"]))
        if d < min_dist:
            return True
    return False


def compute_formation_paths(current_assignments, target_positions):
    """
    For each callsign compute a cubic bezier from current → target.
    Attempts to bend control points outward to avoid 10m conflicts with
    other paths and target positions. Returns {callsign: {c1,c2,color}}.
    """
    paths = {}
    BEND_OFFSETS = [0, 30, -30, 60, -60, 90, -90]   # metres of lateral bend to try

    for cs, tgt in target_positions.items():
        if cs not in current_assignments:
            continue
        src = current_assignments[cs]
        p0 = (src["x"], src["y"], src["z"])
        p1 = (tgt["x"], tgt["y"], tgt["z"])

        dx, dy, dz = p1[0]-p0[0], p1[1]-p0[1], p1[2]-p0[2]
        color = _dominant_color(dx, dy, dz)

        # Perpendicular axis for bending — prefer Z-up, fall back to X
        perp = (0, 0, 1) if abs(dx) > 0.01 or abs(dy) > 0.01 else (1, 0, 0)

        chosen_c1 = _vec3_add(p0, _vec3_scale((dx/3, dy/3, dz/3), 1))
        chosen_c2 = _vec3_add(p0, _vec3_scale((dx*2/3, dy*2/3, dz*2/3), 1))

        for bend in BEND_OFFSETS:
            offset = _vec3_scale(perp, bend)
            c1 = _vec3_add(_vec3_add(p0, _vec3_scale((dx/3, dy/3, dz/3), 1)), offset)
            c2 = _vec3_add(_vec3_add(p0, _vec3_scale((dx*2/3, dy*2/3, dz*2/3), 1)), offset)

            # Check against already-placed paths
            conflict = False
            for other_cs, op in paths.items():
                other_src = current_assignments[other_cs]
                other_tgt = target_positions[other_cs]
                op0 = (other_src["x"], other_src["y"], other_src["z"])
                op1 = (other_tgt["x"], other_tgt["y"], other_tgt["z"])
                oc1 = tuple(op["c1"])
                oc2 = tuple(op["c2"])
                if _paths_conflict(p0, c1, c2, p1, op0, oc1, oc2, op1):
                    conflict = True
                    break

            # Check sampled points against target positions
            if not conflict:
                for t_sample in [i/10 for i in range(1, 10)]:
                    pt = _bezier_point(t_sample, p0, c1, c2, p1)
                    if _point_near_targets(pt, target_positions, cs):
                        conflict = True
                        break

            if not conflict:
                chosen_c1, chosen_c2 = c1, c2
                break

        paths[cs] = {
            "c1":    list(chosen_c1),
            "c2":    list(chosen_c2),
            "color": color
        }

    return paths


# ─── WEBSOCKET HANDLER ────────────────────────────────────────────────────────

async def handle_websocket(websocket):
    CONNECTED_MAPS.add(websocket)
    try:
        await websocket.send(json.dumps(build_sync_all()))

        async for raw in websocket:
            data = json.loads(raw)
            mtype = data.get("type")

            # ── CAPTAIN REGISTRATION ─────────────────────────────────────────
            if mtype == "INITIALIZE_CAPTAIN":
                callsign = data.get("callsign")
                ship     = data.get("ship",  "Unknown")
                color    = data.get("color", "#00ff66")
                slot     = data.get("slot",  "Escort")

                CONNECTED_CAPTAINS[websocket] = callsign
                CONNECTED_MAPS.discard(websocket)

                if callsign not in SYSTEM_DATA["assignments"]:
                    SYSTEM_DATA["assignments"][callsign] = {
                        "ship": ship, "slot": slot,
                        "x": 0, "y": 0, "z": 0,
                        "size": "MEDIUM", "shape": "ARROW", "color": color,
                        "inPosition": False
                    }
                    save_state_to_disk()
                else:
                    SYSTEM_DATA["assignments"][callsign]["color"] = color
                    SYSTEM_DATA["assignments"][callsign]["ship"]  = ship
                    SYSTEM_DATA["assignments"][callsign].setdefault("inPosition", False)

                # Init captain meta
                captain_meta.setdefault(callsign, {
                    "nearbyRange": 500,
                    "view":        {"yaw": 0.1, "pitch": -0.1, "zoom": 1.0},
                    "inPosition":  False
                })
                # Init order state
                pending_order["captain_states"].setdefault(callsign, "IDLE")

                print(f"--> Captain joined: {callsign} ({ship})")
                await broadcast_to_all({
                    "type":           "CAPTAIN_JOINED",
                    "callsign":       callsign,
                    "assignments":    SYSTEM_DATA["assignments"],
                    "onlineCaptains": list(CONNECTED_CAPTAINS.values()),
                    "captainStates":  pending_order["captain_states"],
                    "captainMeta":    captain_meta
                })

            # ── TOKEN DRAG ───────────────────────────────────────────────────
            elif mtype == "TOKEN_MOVE_3D":
                callsign = data.get("callsign")
                if callsign in SYSTEM_DATA["assignments"]:
                    for axis in ["x", "y", "z"]:
                        if axis in data:
                            SYSTEM_DATA["assignments"][callsign][axis] = data[axis]
                    save_state_to_disk()
                    await broadcast_to_all({"type": "FORMATION_SHIFT", "assignments": SYSTEM_DATA["assignments"]})

            # ── SHIP CONFIG UPDATE ───────────────────────────────────────────
            elif mtype == "UPDATE_SHIP_CONFIG":
                callsign = data.get("callsign")
                if callsign in SYSTEM_DATA["assignments"]:
                    for field in ["ship", "size", "shape"]:
                        if field in data:
                            SYSTEM_DATA["assignments"][callsign][field] = data.get(field if field != "ship" else "name", data.get(field))
                    if "name" in data:
                        SYSTEM_DATA["assignments"][callsign]["ship"] = data["name"]
                    save_state_to_disk()
                    await broadcast_to_all({"type": "FORMATION_SHIFT", "assignments": SYSTEM_DATA["assignments"]})

            # ── ADD SHIP SLOT ────────────────────────────────────────────────
            elif mtype == "ADD_SHIP":
                callsign = data.get("callsign", "").strip()
                if callsign and callsign not in SYSTEM_DATA["assignments"]:
                    SYSTEM_DATA["assignments"][callsign] = {
                        "ship":       data.get("ship",  "Unknown"),
                        "slot":       data.get("slot",  "Escort"),
                        "x":          data.get("x",     0),
                        "y":          data.get("y",     0),
                        "z":          data.get("z",     0),
                        "size":       data.get("size",  "MEDIUM"),
                        "shape":      data.get("shape", "ARROW"),
                        "color":      data.get("color", "#00ff66"),
                        "inPosition": False
                    }
                    pending_order["captain_states"].setdefault(callsign, "IDLE")
                    save_state_to_disk()
                    await broadcast_to_all({"type": "FORMATION_SHIFT", "assignments": SYSTEM_DATA["assignments"]})

            # ── REMOVE SHIP SLOT ─────────────────────────────────────────────
            elif mtype == "REMOVE_SHIP":
                callsign = data.get("callsign")
                if callsign in SYSTEM_DATA["assignments"]:
                    del SYSTEM_DATA["assignments"][callsign]
                    pending_order["captain_states"].pop(callsign, None)
                    captain_meta.pop(callsign, None)
                    save_state_to_disk()
                    await broadcast_to_all({"type": "FORMATION_SHIFT", "assignments": SYSTEM_DATA["assignments"]})

            # ── KICK CAPTAIN ─────────────────────────────────────────────────
            elif mtype == "KICK_CAPTAIN":
                target = data.get("callsign")
                target_ws = next((ws for ws, cs in CONNECTED_CAPTAINS.items() if cs == target), None)
                if target_ws:
                    await target_ws.send(json.dumps({"type": "KICKED", "reason": "Removed by Commander"}))
                    await target_ws.close()

            # ── COMMANDER OVERRIDE ───────────────────────────────────────────
            elif mtype == "COMMANDER_OVERRIDE":
                callsign = data.get("callsign")
                if callsign in SYSTEM_DATA["assignments"]:
                    for field in ["ship", "slot", "size", "shape", "color"]:
                        if field in data:
                            SYSTEM_DATA["assignments"][callsign][field] = data[field]
                    save_state_to_disk()
                    await broadcast_to_all({"type": "FORMATION_SHIFT", "assignments": SYSTEM_DATA["assignments"]})

            # ── THEME ────────────────────────────────────────────────────────
            elif mtype == "SAVE_THEME_CONFIG":
                SYSTEM_DATA["colors"] = data.get("colors")
                save_state_to_disk()
                await broadcast_to_all({"type": "THEME_UPDATED", "colors": SYSTEM_DATA["colors"]})

            # ── GRID CONFIG ──────────────────────────────────────────────────
            elif mtype == "SAVE_GRID_CONFIG":
                SYSTEM_DATA["gridConfig"] = data.get("gridConfig")
                save_state_to_disk()
                await broadcast_to_all({"type": "GRID_UPDATED", "gridConfig": SYSTEM_DATA["gridConfig"]})

            # ── SHIP LIBRARY ─────────────────────────────────────────────────
            elif mtype == "SAVE_SHIP_LIBRARY":
                SYSTEM_DATA["shipLibrary"] = data.get("shipLibrary", {})
                save_state_to_disk()
                await broadcast_to_all({"type": "SHIP_LIBRARY_UPDATED", "shipLibrary": SYSTEM_DATA["shipLibrary"]})

            # ── UI LAYOUT ────────────────────────────────────────────────────
            elif mtype == "SAVE_UI_LAYOUT":
                SYSTEM_DATA["uiLayout"] = data.get("uiLayout", {})
                save_state_to_disk()

            # ── FORMATION SELECT ─────────────────────────────────────────────
            elif mtype == "SELECT_FORMATION":
                name = data.get("name")
                if name in SYSTEM_DATA["formations"]:
                    SYSTEM_DATA["activeFormation"] = name
                    SYSTEM_DATA["assignments"] = copy.deepcopy(SYSTEM_DATA["formations"][name])
                    save_state_to_disk()
                    await broadcast_to_all(build_sync_all())

            # ── SAVE FORMATION ───────────────────────────────────────────────
            elif mtype == "SAVE_FORMATION":
                name = data.get("name")
                if name:
                    SYSTEM_DATA["formations"][name] = copy.deepcopy(SYSTEM_DATA["assignments"])
                    SYSTEM_DATA["activeFormation"] = name
                    save_state_to_disk()
                    await broadcast_to_all(build_sync_all())

            # ── DELETE FORMATION ─────────────────────────────────────────────
            elif mtype == "DELETE_FORMATION":
                name = data.get("name")
                if name and name in SYSTEM_DATA["formations"]:
                    del SYSTEM_DATA["formations"][name]
                    if SYSTEM_DATA["activeFormation"] == name:
                        if SYSTEM_DATA["formations"]:
                            SYSTEM_DATA["activeFormation"] = next(iter(SYSTEM_DATA["formations"]))
                            SYSTEM_DATA["assignments"] = copy.deepcopy(
                                SYSTEM_DATA["formations"][SYSTEM_DATA["activeFormation"]])
                        else:
                            SYSTEM_DATA["activeFormation"] = "None"
                            SYSTEM_DATA["assignments"] = {}
                    save_state_to_disk()
                    await broadcast_to_all(build_sync_all())

            # ── ISSUE FORMATION ORDER ────────────────────────────────────────
            elif mtype == "ISSUE_FORMATION_ORDER":
                target_name = data.get("formation")
                if target_name not in SYSTEM_DATA["formations"]:
                    continue

                targets = SYSTEM_DATA["formations"][target_name]
                paths   = compute_formation_paths(SYSTEM_DATA["assignments"], targets)

                pending_order["active"]     = True
                pending_order["formation"]  = target_name
                pending_order["targets"]    = {cs: {"x": t["x"], "y": t["y"], "z": t["z"]}
                                               for cs, t in targets.items()}
                pending_order["paths"]      = paths
                # Reset all captain states to IDLE for this order
                for cs in SYSTEM_DATA["assignments"]:
                    pending_order["captain_states"][cs] = "IDLE"

                print(f"--> Formation order issued: {target_name}")
                await broadcast_to_all({
                    "type":          "FORMATION_ORDER",
                    "formation":     target_name,
                    "targets":       pending_order["targets"],
                    "paths":         paths,
                    "captainStates": pending_order["captain_states"]
                })

            # ── CAPTAIN ACKNOWLEDGE ──────────────────────────────────────────
            elif mtype == "CAPTAIN_ACKNOWLEDGE":
                callsign = CONNECTED_CAPTAINS.get(websocket)
                if callsign and pending_order["active"]:
                    pending_order["captain_states"][callsign] = "ACKNOWLEDGED"
                    print(f"--> {callsign}: ACKNOWLEDGED")
                    await broadcast_to_all({
                        "type":          "CAPTAIN_STATE_CHANGE",
                        "callsign":      callsign,
                        "state":         "ACKNOWLEDGED",
                        "captainStates": pending_order["captain_states"]
                    })

            # ── CAPTAIN READY ────────────────────────────────────────────────
            elif mtype == "CAPTAIN_READY":
                callsign = CONNECTED_CAPTAINS.get(websocket)
                if callsign and pending_order["active"]:
                    pending_order["captain_states"][callsign] = "READY"
                    SYSTEM_DATA["assignments"][callsign]["inPosition"] = True

                    # Move this captain to their target position in assignments
                    if callsign in pending_order["targets"]:
                        tgt = pending_order["targets"][callsign]
                        SYSTEM_DATA["assignments"][callsign]["x"] = tgt["x"]
                        SYSTEM_DATA["assignments"][callsign]["y"] = tgt["y"]
                        SYSTEM_DATA["assignments"][callsign]["z"] = tgt["z"]

                    save_state_to_disk()
                    print(f"--> {callsign}: READY / IN POSITION")

                    # Check if all captains are ready
                    all_ready = all(
                        v == "READY"
                        for cs, v in pending_order["captain_states"].items()
                        if cs in CONNECTED_CAPTAINS.values()
                    )

                    await broadcast_to_all({
                        "type":          "CAPTAIN_STATE_CHANGE",
                        "callsign":      callsign,
                        "state":         "READY",
                        "captainStates": pending_order["captain_states"],
                        "assignments":   SYSTEM_DATA["assignments"],
                        "pingCommander": True   # triggers audio ping + notification on tactical map
                    })

                    if all_ready:
                        # Order complete — clear pending state
                        pending_order["active"]    = False
                        pending_order["formation"] = None
                        pending_order["targets"]   = {}
                        pending_order["paths"]     = {}
                        for cs in pending_order["captain_states"]:
                            pending_order["captain_states"][cs] = "IDLE"
                        for cs in SYSTEM_DATA["assignments"]:
                            SYSTEM_DATA["assignments"][cs]["inPosition"] = False
                        save_state_to_disk()

                        SYSTEM_DATA["activeFormation"] = target_name if (target_name := pending_order.get("formation")) else SYSTEM_DATA["activeFormation"]
                        await broadcast_to_all({
                            "type":        "FORMATION_COMPLETE",
                            "formation":   SYSTEM_DATA["activeFormation"],
                            "assignments": SYSTEM_DATA["assignments"]
                        })

            # ── CAPTAIN VIEW UPDATE (overlay camera) ─────────────────────────
            elif mtype == "UPDATE_CAPTAIN_VIEW":
                callsign = CONNECTED_CAPTAINS.get(websocket)
                if callsign:
                    captain_meta.setdefault(callsign, {})["view"] = {
                        "yaw":   data.get("yaw",   0.1),
                        "pitch": data.get("pitch", -0.1),
                        "zoom":  data.get("zoom",  1.0)
                    }
                    # Broadcast view to all so others can show this captain's perspective indicator
                    await broadcast_to_all({
                        "type":     "CAPTAIN_VIEW_UPDATE",
                        "callsign": callsign,
                        "view":     captain_meta[callsign]["view"]
                    })

            # ── CAPTAIN NEARBY RANGE ─────────────────────────────────────────
            elif mtype == "SET_NEARBY_RANGE":
                callsign = CONNECTED_CAPTAINS.get(websocket)
                if callsign:
                    captain_meta.setdefault(callsign, {})["nearbyRange"] = data.get("range", 500)

            # ── RESET TO DEFAULTS ────────────────────────────────────────────
            elif mtype == "RESET_TO_DEFAULTS":
                SYSTEM_DATA["colors"] = {
                    "hud": "#00f0ff", "lead": "#ff0055",
                    "escort": "#00ff66", "grid": "#1a3d54", "bg": "#01060f"
                }
                SYSTEM_DATA["shipLibrary"] = {}
                SYSTEM_DATA["gridConfig"]  = {"squaresX":4,"squaresY":4,"squaresZ":2,"squareSizeMeters":100}
                SYSTEM_DATA["uiLayout"]    = {}
                save_state_to_disk()
                await broadcast_to_all(build_sync_all())

    finally:
        callsign = CONNECTED_CAPTAINS.pop(websocket, None)
        CONNECTED_MAPS.discard(websocket)
        if callsign:
            print(f"--> Captain left: {callsign}")
            await broadcast_to_all({
                "type":           "CAPTAIN_LEFT",
                "callsign":       callsign,
                "onlineCaptains": list(CONNECTED_CAPTAINS.values()),
                "captainStates":  pending_order["captain_states"]
            })


# ─── HTTP ─────────────────────────────────────────────────────────────────────

async def serve_tactical_page(request):
    html_path = os.path.join(os.path.dirname(__file__), "tactical.html")
    if os.path.exists(html_path):
        return web.FileResponse(html_path)
    return web.Response(text="tactical.html missing", status=404)


async def main():
    app = web.Application()
    app.router.add_get("/tactical", serve_tactical_page)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()

    print("Commander Server running:")
    print("  Tactical Map -> http://localhost:8080/tactical")
    print("  WebSocket    -> ws://0.0.0.0:8081")

    async with websockets.serve(handle_websocket, "0.0.0.0", 8081):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
