# MaximizePM desktop app

The dashboard in its own window. The app runs `maxpm serve` on a free port on
127.0.0.1, shows the page, and stops the server when you quit.

The released app carries its own Python (CPython 3.13 from
python-build-standalone, one build for Apple silicon and one for Intel), so it
needs nothing installed. Run from this repo (`npm start`), it uses Python 3.10
or newer from the machine (PATH, then the usual Homebrew and system places)
and says so if Python is missing. It uses
the same queue as the `maxpm` command (`MAXPM_DB`, else
`~/.maximizepm/maxpm.db`).

```
cd desktop
npm install
npm start      # run the app from this repo
npm test       # smoke test: starts maxpm serve from the repo and stops it
```

Agents that the app starts run the `maxpm` command. On a Mac with only the
app, the setup guide's "Let agents use MaximizePM" step writes a small launcher
to `~/.local/bin/maxpm` (it runs the app's own Python and MaximizePM) and, when a
new terminal would not find it, adds `~/.local/bin` to PATH in `~/.zprofile`.
A `maxpm` command you installed yourself (a clone or pip) is left alone.

Inside the app the page's git Update button is hidden: the server runs with
`MAXPM_DESKTOP=1`, and the app gets its updates as a new release.

## Updates

Once a day `maxpm serve` asks GitHub for the latest release
(`river/release.py`; setting `update_check`). When a newer version is out, the
page shows a bar with its notes and one button:

- Most releases change only the MaximizePM code. Update downloads the
  release's code archive, checks its sha256 (from the release's
  `manifest.json`), unpacks it to `~/.maximizepm/app/versions/<version>`,
  checks that it loads, and starts `maxpm serve` again on it. The `.app` does
  not change, so macOS asks nothing. At each start the app runs the newest
  good version: a version that does not start is marked `.bad`, and the app
  goes back to the one before (in the end, its own copy).
- A release whose app shell changed (Electron, `main.js`, `server.js`,
  `preload.js`, the build settings or the Python: `scripts/shell-id.js`) has
  another shell id in its manifest. Then the button downloads the new `.dmg`
  to Downloads and opens it.

The `maxpm` launcher in `~/.local/bin` runs the version that
`~/.maximizepm/app/current` names.

## Release

`npm run dist` builds `out/MaximizePM-<version>-arm64.dmg` and `-x64.dmg`
with electron-builder. The app carries a copy of the command and its Python
package, and `scripts/fetch-python.js` (run first; pinned release and sha256)
puts the Python for each chip type in `build/python/<os>-<arch>/` (`mac-arm64`,
`mac-x64`). Pushing a version tag (`git tag v0.1.0 && git push origin v0.1.0`)
runs `.github/workflows/desktop-release.yml`, which builds the `.dmg` files on
macOS, checks that each app starts MaximizePM from its own copy, and attaches
both to the GitHub Release for that tag, with `release-notes.md` as its notes.
Until the signing secrets are set, the app is signed ad hoc only, so macOS
says it "could not verify" the app. `release-notes.md` tells people how to open
it: Open Anyway in System Settings > Privacy & Security (or the `xattr`
command, which also stops the slow check before each start).

## Signing and notarizing

With the five secrets `APPLE_CERT_P12`, `APPLE_CERT_PASSWORD`,
`APPLE_API_KEY_P8`, `APPLE_API_KEY_ID` and `APPLE_API_ISSUER` in the GitHub
repository, the release builds with `npm run dist:signed`
(`signed.config.js`): electron-builder signs every program of the app (the
Electron files, the bundled Python and its libraries, the dictation helper)
with the Developer ID and the hardened runtime, with the exceptions in
`entitlements.mac.plist`, then sends the app to Apple's notary service and
staples the ticket. The workflow then notarizes and staples the `.dmg` files,
and checks each app and `.dmg` with `spctl`. With none of the secrets, it
builds as before (`npm run dist`, signed ad hoc); with only some, it stops.

To set the secrets:

1. The certificate request is `~/.maximizepm/signing/developer-id.csr`; its
   private key (`developer-id.key`, mode 600) stays on this Mac. At
   developer.apple.com, Certificates, make a Developer ID Application
   certificate from the request, and download the `.cer` file.
2. In App Store Connect, Users and Access, Integrations, Team Keys, make an API
   key with the Developer role. Download its `.p8` file, and note its key id
   and the issuer id.
3. Run `desktop/scripts/signing-secrets.sh <the .cer> <the .p8> <key id>
   <issuer id>`. It makes a `.p12` with a new password, sets the five secrets
   with `gh secret set`, and prints only their names.

The desktop app is for macOS. On Windows, install WSL 2 and run MaximizePM
there: see [Windows](../README.md#windows) in the main README.
