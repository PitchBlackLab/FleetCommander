import asyncio
import json
import os
import copy
from aiohttp import web
import websockets
from pynput import keyboard

CONNECTED_CAPTAINS = {}  # Format: {websocket: callsign}
CONNECTED_MAPS = set()
CONFIG_FILE = "formations.json"

# AUTHORITATIVE GLOBAL STORAGE (SYSTEM SEED DEFAULTS)
SYSTEM_DATA = {
    "colors": {
        "hud": "#00f0ff",
        "lead": "#ff0055",
        "escort": "#00ff66",
        "grid": "#1a3d54",
        "bg": "#01060f"
    },
    "activeFormation": "Vanguard-Wedge",
    "formations": {
        "Vanguard-Wedge": {
            "Titan-1": {"ship": "Javelin", "slot": "Lead", "x": 0, "y": 0, "z": 0, "size": "LARGE", "shape": "WEDGE"},
            "Hammer-1": {"ship": "Hammerhead", "slot": "Port-Screen", "x": -80, "y": -60, "z": 0, "size": "MEDIUM", "shape": "ARROW"},
            "Hammer-2": {"ship": "Hammerhead", "slot": "Starboard-Screen", "x": 80, "y": -60, "z": 0, "size": "MEDIUM", "shape": "ARROW"}
        },
        "Suppression-Line": {
            "Titan-1": {"ship": "Javelin", "slot": "Lead", "x": 0, "y": 0, "z": 0, "size": "LARGE", "shape": "WEDGE"},
            "Hammer-1": {"ship": "Hammerhead", "slot": "Port-Screen", "x": -150, "y": 0, "z": 20, "size": "MEDIUM", "shape": "ARROW"},
            "Hammer-2": {"ship": "Hammerhead", "slot": "Starboard-Screen", "x": 150, "y": 0, "z": -20, "size": "MEDIUM", "shape": "ARROW"}
        }
    },
    "assignments": {}  # Populated dynamically by active system profile loader
}

# FILE OPERATION IO ENGINE PIPELINES
def load_state_from_disk():
    global SYSTEM_DATA
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded = json.load(f)
                # Verify structural components to prevent disk corruption crash loops
                if all(k in loaded for k in ["colors", "activeFormation", "formations", "assignments"]):
                    SYSTEM_DATA = loaded
                    print(f"--> SUCCESS: Loaded active system configurations from {CONFIG_FILE}")
                    return
        except Exception as e:
            print(f"--> ERROR: Failed reading {CONFIG_FILE}: {e}. Falling back to system default seeds.")
    
    # Fallback/Seed Initialization Logic
    SYSTEM_DATA["assignments"] = copy.deepcopy(SYSTEM_DATA["formations"][SYSTEM_DATA["activeFormation"]])
    save_state_to_disk()

def save_state_to_disk():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(SYSTEM_DATA, f, indent=4)
        print(f"--> SUCCESS: Flushed complete active system configurations state matrix to {CONFIG_FILE}")
    except Exception as e:
        print(f"--> ERROR: Failed flushing configurations matrix state parameters to disk: {e}")

# Initial validation load routine run on execution start
load_state_from_disk()

fleet_vectors = {"pitch": "NEUTRAL", "yaw": "NEUTRAL", "roll": "NEUTRAL", "vertical": "NEUTRAL"}
loop = None

async def broadcast_to_all(data_dict):
    message = json.dumps(data_dict)
    all_targets = list(CONNECTED_CAPTAINS.keys()) + list(CONNECTED_MAPS)
    if all_targets:
        await asyncio.gather(*[client.send(message) for client in all_targets], return_exceptions=True)

async def handle_websocket(websocket):
    CONNECTED_MAPS.add(websocket)
    try:
        # Deliver complete structural configuration state parameters to client immediately upon connection hook handshakes
        await websocket.send(json.dumps({
            "type": "SYNC_ALL", 
            "vectors": fleet_vectors, 
            "formation": SYSTEM_DATA["activeFormation"], 
            "assignments": SYSTEM_DATA["assignments"],
            "colors": SYSTEM_DATA["colors"],
            "formationList": list(SYSTEM_DATA["formations"].keys())
        }))
        
        async for message in websocket:
            data = json.loads(message)
            
            if data.get("type") == "INITIALIZE_CAPTAIN":
                callsign = data.get("callsign")
                CONNECTED_CAPTAINS[websocket] = callsign
                if websocket in CONNECTED_MAPS: CONNECTED_MAPS.remove(websocket)
                print(f"Captain registered: {callsign}")
            
            elif data.get("type") == "TOKEN_MOVE_3D":
                callsign = data.get("callsign")
                if callsign in SYSTEM_DATA["assignments"]:
                    for axis in ['x', 'y', 'z']:
                        if axis in data: SYSTEM_DATA["assignments"][callsign][axis] = data[axis]
                    await broadcast_to_all({"type": "FORMATION_SHIFT", "assignments": SYSTEM_DATA["assignments"]})

            elif data.get("type") == "UPDATE_SHIP_CONFIG":
                callsign = data.get("callsign")
                if callsign in SYSTEM_DATA["assignments"]:
                    SYSTEM_DATA["assignments"][callsign]["ship"] = data.get("name")
                    SYSTEM_DATA["assignments"][callsign]["size"] = data.get("size")
                    SYSTEM_DATA["assignments"][callsign]["shape"] = data.get("shape")
                    await broadcast_to_all({"type": "FORMATION_SHIFT", "assignments": SYSTEM_DATA["assignments"]})

            elif data.get("type") == "SAVE_THEME_CONFIG":
                # Commit updated interface color specifications state parameters
                SYSTEM_DATA["colors"] = data.get("colors")
                save_state_to_disk()
                await broadcast_to_all({"type": "THEME_UPDATED", "colors": SYSTEM_DATA["colors"]})

            elif data.get("type") == "SELECT_FORMATION":
                name = data.get("name")
                if name in SYSTEM_DATA["formations"]:
                    SYSTEM_DATA["activeFormation"] = name
                    SYSTEM_DATA["assignments"] = copy.deepcopy(SYSTEM_DATA["formations"][name])
                    save_state_to_disk()
                    await broadcast_to_all({
                        "type": "SYNC_ALL", 
                        "vectors": fleet_vectors, 
                        "formation": SYSTEM_DATA["activeFormation"], 
                        "assignments": SYSTEM_DATA["assignments"],
                        "colors": SYSTEM_DATA["colors"],
                        "formationList": list(SYSTEM_DATA["formations"].keys())
                    })

            elif data.get("type") == "SAVE_FORMATION":
                name = data.get("name")
                # Append a deep copy of the currently dragged assignments layout into the permanent matrix configuration dict templates
                SYSTEM_DATA["formations"][name] = copy.deepcopy(SYSTEM_DATA["assignments"])
                SYSTEM_DATA["activeFormation"] = name
                save_state_to_disk()
                await broadcast_to_all({
                    "type": "SYNC_ALL", 
                    "vectors": fleet_vectors, 
                    "formation": SYSTEM_DATA["activeFormation"], 
                    "assignments": SYSTEM_DATA["assignments"],
                    "colors": SYSTEM_DATA["colors"],
                    "formationList": list(SYSTEM_DATA["formations"].keys())
                })
    finally:
        if websocket in CONNECTED_CAPTAINS: del CONNECTED_CAPTAINS[websocket]
        if websocket in CONNECTED_MAPS: CONNECTED_MAPS.remove(websocket)

async def serve_tactical_page(request):
    html_path = os.path.join(os.path.dirname(__file__), 'tactical.html')
    if os.path.exists(html_path): return web.FileResponse(html_path)
    return web.Response(text="tactical.html file missing", status=404)

async def main():
    global loop
    loop = asyncio.get_running_loop()
    
    app = web.Application()
    app.router.add_get('/tactical', serve_tactical_page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    
    print("Voxel Persistence UI operating on http://localhost:8080/tactical")
    async with websockets.serve(handle_websocket, "0.0.0.0", 8081):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
