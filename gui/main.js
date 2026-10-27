// Guban Studio main process: owns the window and the Python worker (uv run python -m guban.gui.worker, repo root).
// The renderer reaches the worker only through preload.js (guban.call / onEvent); see src/guban/gui/worker.py for
// the JSON-lines protocol. --smoke: run the renderer's smoke steps hidden, save a screenshot per step to
// reports/_gui_smoke/, write result.json there and exit 0 (all passed) or 1.
const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const { spawn, execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const SMOKE = process.argv.includes('--smoke');
const SMOKE_DIR = path.join(ROOT, 'reports', '_gui_smoke');
const STDERR_LINES = 40;

// One Python worker process. "main" holds the tabs' state; "opt" runs optimizations, started on its first call, so
// Stop can end it (pool processes included) without losing the main worker's data.
class Worker {
  constructor(name, onEvent, onExit) {
    this.name = name;
    this.onEvent = onEvent;
    this.onExit = onExit;
    this.pending = new Map();
    this.nextId = 1;
    this.proc = null;
  }

  start() {
    const env = { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' };
    this.proc = spawn('uv', ['run', '--quiet', 'python', '-m', 'guban.gui.worker'], { cwd: ROOT, env, windowsHide: true });
    this.stderr = [];
    let buf = '';
    this.proc.stdout.setEncoding('utf8');
    this.proc.stdout.on('data', (chunk) => {
      buf += chunk;
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (line.trim()) this.receive(line);
      }
    });
    this.proc.stderr.setEncoding('utf8');
    this.proc.stderr.on('data', (chunk) => {
      process.stderr.write(chunk);
      this.stderr.push(...chunk.split(/\r?\n/).filter(Boolean));
      this.stderr = this.stderr.slice(-STDERR_LINES);
    });
    const proc = this.proc;
    proc.on('error', (e) => this.stopped(proc, null, `Could not start uv: ${e.message}`));
    proc.on('exit', (code) => this.stopped(proc, code));
  }

  receive(line) {
    let msg;
    try {
      msg = JSON.parse(line);
    } catch (e) {
      this.stderr.push(`(not a protocol line) ${line.slice(0, 200)}`);
      return;
    }
    if (msg.event) return this.onEvent(msg);
    const p = this.pending.get(msg.id);
    if (p) {
      this.pending.delete(msg.id);
      p.resolve(msg);
    }
  }

  stopped(proc, code, why) {
    if (proc !== this.proc) return; // an old process from before a restart
    this.proc = null;
    const stderr = [...(why ? [why] : []), ...this.stderr];
    for (const p of this.pending.values()) p.resolve({ ok: false, error: { message: 'Python worker stopped', frames: [], traceback: stderr.join('\n') } });
    this.pending.clear();
    this.onExit({ code, stderr });
  }

  call(method, params) {
    if (!this.proc && this.name !== 'main') this.start();
    if (!this.proc) return Promise.resolve({ ok: false, error: { message: 'Python worker is not running: click Restart', frames: [] } });
    const id = this.nextId++;
    return new Promise((resolve) => {
      this.pending.set(id, { resolve });
      this.proc.stdin.write(JSON.stringify({ id, method, params: params || {} }) + '\n');
    });
  }

  kill() {
    const proc = this.proc;
    if (!proc) return;
    this.proc = null;
    for (const p of this.pending.values()) p.resolve({ ok: false, error: { message: 'Stopped', frames: [], stopped: true } });
    this.pending.clear();
    // uv starts python as its own child: end the whole tree, or python outlives the window
    if (process.platform === 'win32') {
      try { execFileSync('taskkill', ['/pid', String(proc.pid), '/T', '/F'], { stdio: 'ignore' }); } catch (e) { /* already gone */ }
    } else {
      proc.kill();
    }
  }
}

let win = null;
const workers = {};

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1000,
    minHeight: 640,
    show: !SMOKE,
    backgroundColor: '#0b0f17',
    title: 'Guban Studio',
    autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: !SMOKE, offscreen: SMOKE },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  if (SMOKE) win.webContents.once('did-finish-load', () => smoke().catch((e) => { console.error(e); app.exit(1); }));
}

async function smoke() {
  fs.mkdirSync(SMOKE_DIR, { recursive: true });
  const steps = await win.webContents.executeJavaScript('studio.smoke.steps()');
  const results = [];
  for (let i = 0; i < steps.length; i++) {
    const r = await win.webContents.executeJavaScript(`studio.smoke.run(${i})`);
    await new Promise((res) => setTimeout(res, 400)); // let images decode and paint
    win.webContents.invalidate();
    await new Promise((res) => setTimeout(res, 200));
    const shot = path.join(SMOKE_DIR, `${String(i + 1).padStart(2, '0')}_${steps[i]}.png`);
    fs.writeFileSync(shot, (await win.webContents.capturePage()).toPNG());
    results.push({ step: steps[i], ...r, screenshot: shot });
    if (!r.ok) break;
  }
  const ok = results.length === steps.length && results.every((r) => r.ok);
  // a GUI-subsystem exe's stdout does not reach the console on Windows: `guban gui --smoke` prints this file
  fs.writeFileSync(path.join(SMOKE_DIR, 'result.json'), JSON.stringify({ ok, results }, null, 2) + '\n');
  killAll();
  app.exit(ok ? 0 : 1);
}

function killAll() {
  for (const w of Object.values(workers)) w.kill();
}

app.whenReady().then(() => {
  for (const name of ['main', 'opt']) {
    workers[name] = new Worker(name, (msg) => send('guban:event', { ...msg, worker: name }), (info) => send('guban:worker-exit', { ...info, worker: name }));
  }
  workers.main.start();
  const pick = (name) => workers[name || 'main'];
  ipcMain.handle('guban:call', (_e, method, params, name) => pick(name).call(method, params));
  ipcMain.handle('guban:restart', (_e, name) => { pick(name).kill(); pick(name).start(); return true; });
  ipcMain.handle('guban:stop', (_e, name) => { pick(name).kill(); return true; });
  ipcMain.handle('guban:pick-file', async (_e, filters) => {
    const r = await dialog.showOpenDialog(win, { defaultPath: ROOT, properties: ['openFile'], filters: filters || [] });
    return r.canceled ? null : r.filePaths[0];
  });
  createWindow();
});

app.on('window-all-closed', () => {
  killAll();
  app.quit();
});
app.on('before-quit', killAll);
