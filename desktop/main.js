// MaximizePM desktop app: runs `maxpm serve` on a free local port and shows the page in a window.
const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const { openRiver, appDir, markBad, pickRoot } = require("./server");

// In development the river folder is the repo around desktop/; a packaged app carries a copy (the bundle). An Update
// of the page (#5601) puts newer versions of the code in the app's folder (~/.maximizepm/app), and the app starts
// the newest good one with the Python of its bundle.
const bundle = app.isPackaged ? path.join(process.resourcesPath, "river-app") : path.join(__dirname, "..");
const dir = appDir();
let river = null, win = null, stopping = false, restarts = [];

// The id of this app shell (scripts/shell-id.js, written at the build): a release with another id needs the new app.
function shellId() {
  try { return fs.readFileSync(path.join(process.resourcesPath, "shell-id.txt"), "utf8").trim(); } catch (e) { return ""; }
}

// A maxpm serve that runs for the queue already (the login serve, or one from a terminal) is opened as it is, and
// quitting the app leaves it running (#5618); else the app starts its own, on the queue's serve_port when it is free.
async function startNewest() {
  if (!app.isPackaged) return openRiver({ riverRoot: bundle });
  const root = pickRoot({ bundle, dir });
  const env = { ...process.env, MAXPM_APP_BUNDLE: bundle, MAXPM_APP_DIR: dir, MAXPM_APP_SHELL: shellId() };
  try {
    return await openRiver({ riverRoot: root, pythonRoot: bundle, env });
  } catch (e) {
    // A version that does not start: mark it bad and start the one before (in the end the app's own copy).
    if (root !== bundle && markBad(root, dir)) return startNewest();
    throw e;
  }
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
      if (win) win.loadURL(river.url);
    } catch (e) {
      dialog.showErrorBox("MaximizePM could not start", e.message);
      app.quit();
    }
  });
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
  e.preventDefault();
  river.stop().finally(() => app.quit());
});
