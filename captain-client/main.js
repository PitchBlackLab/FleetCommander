const { app, BrowserWindow, screen, ipcMain, globalShortcut } = require('electron');
const path = require('path');

let win = null;

function createOverlayWindow() {
    const primaryDisplay = screen.getPrimaryDisplay();
    const { width, height } = primaryDisplay.workAreaSize;

    win = new BrowserWindow({
        width,
        height,
        x: 0,
        y: 0,
        transparent: true,
        frame: false,
        alwaysOnTop: true,
        hasShadow: false,
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            preload: path.join(__dirname, 'preload.js')
        }
    });

    // Start with mouse events ENABLED — setup screen needs full interaction
    win.setIgnoreMouseEvents(false);

    win.loadFile('index.html');
}

// Toggle click-through from renderer
ipcMain.on('set-ignore-mouse', (_event, ignore) => {
    if (win) {
        if (ignore) {
            // forward: true lets Electron still detect mousemove for future toggling
            win.setIgnoreMouseEvents(true, { forward: true });
        } else {
            win.setIgnoreMouseEvents(false);
        }
    }
});

app.whenReady().then(() => {
    createOverlayWindow();

    // RAlt+Shift+M — cycle ACK → READY for the active formation order
    globalShortcut.register('Shift+AltGr+M', () => {
        if (win) win.webContents.send('hotkey', 'ACK_READY');
    });

    // RAlt+Shift+V — toggle interactive mode on the captain's 3D overlay map
    globalShortcut.register('Shift+AltGr+V', () => {
        if (win) win.webContents.send('hotkey', 'TOGGLE_MAP');
    });

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) createOverlayWindow();
    });
});

app.on('will-quit', () => globalShortcut.unregisterAll());

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
});
