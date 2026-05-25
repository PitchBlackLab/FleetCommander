const { contextBridge, ipcRenderer } = require('electron');

// Expose a safe bridge to the renderer
contextBridge.exposeInMainWorld('electronAPI', {
    setIgnoreMouse: (ignore) => ipcRenderer.send('set-ignore-mouse', ignore),
    onHotkey: (cb) => ipcRenderer.on('hotkey', (_e, key) => cb(key))
});
