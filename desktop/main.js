// MaximizePM desktop app: runs `maxpm serve` on a free local port and shows the page in a window.
const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const path = require("node:path");
const { startRiver } = require("./server");

// In development the river folder is the repo around desktop/; a packaged app carries a copy.
const riverRoot = app.isPackaged ? path.join(process.resourcesPath, "river-app") : path.join(__dirname, "..");
let river = null;

async function open() {
  try {
    river = await startRiver({ riverRoot });
  } catch (e) {
    dialog.showErrorBox("MaximizePM could not start", e.message);
    app.quit();
    return;
  }
  console.log(`MaximizePM app: ${river.url}`);
  // The app icon in the Dock when the app runs from source; a build has it in its bundle.
  const icon = path.join(__dirname, "icon.png");
  if (!app.isPackaged && app.dock) app.dock.setIcon(icon);
  const win = new BrowserWindow({ width: 1400, height: 900, title: "MaximizePM", icon,
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
let stopping = false;
app.on("will-quit", (e) => {
  if (!river || stopping) return;
  stopping = true;
  e.preventDefault();
  river.stop().finally(() => app.quit());
});
