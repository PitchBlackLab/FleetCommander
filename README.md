# FleetCommander
# Create the root project folder
mkdir "Fleet Commander"
cd "Fleet Commander"

# Setup the Commander Server directory and dependencies
mkdir commander-server
cd commander-server
pip install websockets pynput
cd ..

# Setup the Captain Client directory and dependencies
mkdir captain-client
cd captain-client

# 1. Clear any accidental caching or bad state
npm cache clean --force

# 2. Install all dependencies and force lifecycle scripts to download the binary
npm install --foreground-scripts

# 3. Explicitly verify the Electron platform binary is downloaded
npx electron-builder install-app-deps

npm init -y
npm install electron@^28.0.0
node node_modules/electron/install.js  
$env:ELECTRON_SKIP_BINARY_DOWNLOAD=""; npm rebuild electron --update-binary



---------------
troubleshooting
https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe
https://nodejs.org/dist/v22.22.3/node-v22.22.3-x64.msi  USE 22.22.3 LTS! check box to install 3rd party apps including chocolatety



# Force Electron to bypass the broken Node 24 install script and pull the binary directly
$env:ELECTRON_SKIP_BINARY_DOWNLOAD="0"
npx --node-version=22.11.0 npm install electron --force


cd sc-fleet-sync\captain-client
node node_modules/electron/install.js  
$env:ELECTRON_SKIP_BINARY_DOWNLOAD=""; npm rebuild electron --update-binary



 py -m venv .venv
 .venv\Scripts\Activate.ps1
 (.venv) PS C:\dev\Fleet_Commander\sc-fleet-sync\commander-server> pip install aiohttp


 
