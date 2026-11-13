// MaximizePM desktop app: runs `maxpm serve` on a free local port and shows the page in a window.
const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const { openRiver, appDir, markBad, pickRoot, bundledPython, runMaxpm, waitDown } = require("./server");

// In development the river folder is the repo around desktop/; a packaged app carries a copy (the bundle). An Update
// of the page (#5601) puts newer versions of the code in the app's folder (~/.maximizepm/app), and the app starts
// the newest good one with the Python of its bundle.
const bundle = app.isPackaged ? path.join(process.resourcesPath, "river-app") : path.join(__dirname, "..");
const dir = appDir();
let river = null, win = null, stopping = false, restarts = [], root = bundle, busy = false;

// The id of this app shell (scripts/shell-id.js, written at the build): a release with another id needs the new app.
function shellId() {
  try { return fs.readFileSync(path.join(process.resourcesPath, "shell-id.txt"), "utf8").trim(); } catch (e) { return ""; }
}

// The variables of the app's serve and of the maxpm commands it runs: river/login.py reads them to write the
// launchd jobs of the app (#5649), which run the app's code and Python.
function appEnv() {
  return { ...process.env, MAXPM_DESKTOP: "1", MAXPM_APP_BUNDLE: bundle, MAXPM_APP_DIR: dir, MAXPM_APP_SHELL: shellId() };
}

// A maxpm serve that runs for the queue already (the login serve, or one from a terminal) is opened as it is, and
// quitting the app leaves it running (#5618); else the app starts its own, on the queue's serve_port when it is free.
// The packaged app also installs or updates the watchdog at each start (appStart, #5649).
async function startNewest() {
  if (!app.isPackaged) return openRiver({ riverRoot: bundle });
  root = pickRoot({ bundle, dir });
  try {
    return await openRiver({ riverRoot: root, pythonRoot: bundle, env: appEnv(), appStart: true });
  } catch (e) {
    // A version that does not start: mark it bad and start the one before (in the end the app's own copy).
    if (root !== bundle && markBad(root, dir)) return startNewest();
    throw e;
  }
}

// While the app is open, <app folder>/open names it, the serve it started and the queue (#5649): the watchdog then
// knows that serve should run, and its repair stops a serve that hangs, which watch() starts again. The app removes
// the file when it quits, before it stops its serve: the watchdog then sends no notice about a serve stopped on purpose.
const marker = path.join(dir, "open");
function markOpen() {
  if (!app.isPackaged || !river) return;
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(marker + ".tmp", JSON.stringify({ app: process.pid, serve: river.child ? river.child.pid : null, db: river.db }) + "\n");
    fs.renameSync(marker + ".tmp", marker);
  } catch (e) { /* the watchdog then runs no check that needs serve */ }
}
function markClosed() {
  try { fs.rmSync(marker, { force: true }); } catch (e) { /* gone already */ }
}

// The server ended while the app runs: an Update that started serve on new code which then failed, or a crash.
// Start the newest good version again (pickRoot marks an Update that never answered as bad), at most 3 times a minute.
function watch(r) {
  if (!r.child) return;  // a serve the app did not start: it is not the app's to start again
  r.child.once("exit", async () => {
    if (stopping || river !== r) return;
    restarts = restarts.filter((t) => Date.now() - t < 60000).concat(Date.now());
    if (restarts.length > 3) {
      dialog.showErrorBox("MaximizePM stopped", "MaximizePM stopped several times in a minute. Open the app again.");
      return app.quit();
    }
    try {
      river = await startNewest();
      watch(river);
      markOpen();
      if (win) win.loadURL(river.url);
    } catch (e) {
      dialog.showErrorBox("MaximizePM could not start", e.message);
      app.quit();
    }
  });
}

// Keep MaximizePM running in the background (#5649): on, a launchd login agent runs the app's serve at login and
// after a crash, with no window, and quitting the app leaves it running; off, the app runs its own serve, which stops
// when the app quits. A login serve that another install made (a git clone's) shows checked and cannot change here.
async function setBackground(on) {
  if (busy) return;
  busy = true;
  buildMenu();
  const old = river;
  const ctx = { riverRoot: root, python: bundledPython(bundle), env: appEnv() };
  stopping = true;  // the app's own serve stops now: watch() must not start it again
  try {
    if (on) {
      if (old && old.child) await old.stop();  // the port is free for the agent's serve
      runMaxpm(ctx, ["serve", "--install-login"]);
    } else {
      runMaxpm(ctx, ["serve", "--remove-login"]);  // launchd stops the agent's serve
      if (old && old.external) await waitDown(old.url, 10000);
    }
  } catch (e) {
    dialog.showErrorBox("MaximizePM", `Keep MaximizePM running in the background: ${e.message}`);
  }
  stopping = false;
  try {
    river = await startNewest();
  } catch (e) {
    dialog.showErrorBox("MaximizePM could not start", e.message);
    return app.quit();
  }
  watch(river);
  markOpen();
  busy = false;
  buildMenu();
  if (win) win.loadURL(river.url);
}

// The app menu: the standard one, with the background setting under the app's name.
function buildMenu() {
  const a = (river && river.app) || {};
  if (a.error) console.log(`MaximizePM app: the watchdog: ${a.error}`);
  const background = { label: "Keep MaximizePM running in the background", type: "checkbox",
    checked: a.background === "on" || a.background === "other",
    enabled: app.isPackaged && !busy && (a.background === "on" || a.background === "off"),
    click: (item) => setBackground(item.checked) };
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: app.name, submenu: [{ role: "about" }, { type: "separator" }, background, { type: "separator" },
      { role: "services" }, { type: "separator" }, { role: "hide" }, { role: "hideOthers" }, { role: "unhide" },
      { type: "separator" }, { role: "quit" }] },
    { role: "fileMenu" }, { role: "editMenu" }, { role: "viewMenu" }, { role: "windowMenu" },
  ]));
}

async function open() {
  try {
    river = await startNewest();
  } catch (e) {
    dialog.showErrorBox("MaximizePM could not start", e.message);
    app.quit();
    return;
  }
  watch(river);
  markOpen();
  buildMenu();
  console.log(`MaximizePM app: ${river.url}${river.external ? " (a maxpm serve that runs already)" : ""}`);
  // The app icon in the Dock when the app runs from source; a build has it in its bundle.
  const icon = path.join(__dirname, "icon.png");
  if (!app.isPackaged && app.dock) app.dock.setIcon(icon);
  win = new BrowserWindow({ width: 1400, height: 900, title: "MaximizePM", icon,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, preload: path.join(__dirname, "preload.js") } });
  // "Add a project folder" on the page: the system folder picker, only for the page river serves.
  ipcMain.handle("river:pick-folder", async (e) => {
    if (!e.senderFrame || !e.senderFrame.url.startsWith(river.url)) return null;
    const r = await dialog.showOpenDialog(win, { title: "Choose a project folder", buttonLabel: "Choose",
      properties: ["openDirectory", "createDirectory"] });
    return r.canceled ? null : r.filePaths[0];
  });
  // Links out of the page (GitHub, session links) open in the browser, not in the app window.
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e, url) => { if (!url.startsWith(river.url)) { e.preventDefault(); shell.openExternal(url); } });
  win.loadURL(river.url);
}

app.whenReady().then(open);
app.on("window-all-closed", () => app.quit());
app.on("will-quit", (e) => {
  if (!river || stopping) return;
  stopping = true;
  markClosed();  // first: the serve stops on purpose now
  e.preventDefault();
  river.stop().finally(() => app.quit());
});
