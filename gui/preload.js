const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('guban', {
  call: (method, params, worker) => ipcRenderer.invoke('guban:call', method, params, worker),
  restart: (worker) => ipcRenderer.invoke('guban:restart', worker),
  stop: (worker) => ipcRenderer.invoke('guban:stop', worker),
  pickFile: (filters) => ipcRenderer.invoke('guban:pick-file', filters),
  onEvent: (fn) => ipcRenderer.on('guban:event', (_e, msg) => fn(msg)),
  onWorkerExit: (fn) => ipcRenderer.on('guban:worker-exit', (_e, info) => fn(info)),
});
