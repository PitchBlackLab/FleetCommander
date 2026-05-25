# Fleet Commander — SC Tactical Overlay System

## Project Structure

```
commander-server/
  server.py         — Python backend (WebSocket + HTTP)
  tactical.html     — Commander's 3D tactical map (browser)
  formations.json   — Persisted fleet state (auto-generated on first run)

captain-client/
  main.js           — Electron entry point
  preload.js        — IPC bridge (click-through toggle)
  index.html        — Captain HUD overlay
  package.json      — Node dependencies
```

## Setup

### Commander Server
**Requirements:** Python 3.10+, pip

```bash
cd commander-server
pip install aiohttp websockets
python server.py
```

Tactical map available at: `http://localhost:8080/tactical`
WebSocket on port `8081`

### Captain Client
**Requirements:** Node.js 20–22

```bash
cd captain-client
npm install
npm start
```

Each captain sets their **Callsign**, **Ship**, **Colour**, and the **Commander's Tailscale IP** on the pre-connect screen before joining.

## Network
All traffic runs over a private **Tailscale** mesh VPN.
- Commander runs `server.py` — captains connect to the Commander's Tailscale IP.
- No ports need to be forwarded. Anti-cheat safe (no inputs touch captain machines).

## Hotkeys (Captain Client)
| Hotkey | Action |
|--------|--------|
| RAlt + Shift + M | Acknowledge formation order (1st press) → Ready / In Position (2nd press) |
| RAlt + Shift + V | Toggle interactive mode on 3D overlay map |

