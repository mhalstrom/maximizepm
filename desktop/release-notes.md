MaximizePM desktop app {TAG}. The app carries its own Python, so you do not have to install anything else.

## What's new in {TAG}

A big one: more than 500 changes since v0.1.8. The main ones:

- **Sessions is the first page.** Each agent session shows as a chat that you can read and type into. You can attach images, dictate, use the agent's slash commands, change a session's model and settings, pin it, or end it. A side panel beside the chat shows the pages, files and screenshots the session makes.
- **The Plan tab.** Items, Goals, Graph, Releases, Done and Health, for one project or all of them. The Board and Capacity tabs are gone.
- **More agents.** Besides Claude Code and Codex, MaximizePM can start and follow Pi, OpenCode, Gemini CLI, GitHub Copilot CLI, Cursor CLI, Grok CLI, Hermes, Aider, Goose and Amp.
- **Canvases.** A session can show you a plan, a report or a draft as a page with versions. You comment on any part of it and answer its decisions right there.
- **Expert sessions.** A session you talk with for days about one topic. It keeps its own notes, so a fresh one can pick up where it stopped.
- **A manager that works for you.** Questions, alerts and items for you go to the manager first, and it brings you only what it can't decide. Policies keep your rules for every session.
- **Releases with less hand work.** A release cadence, cut releases, reviews that always start in a fresh session, hooks on release events, and a deploy hook that ships with no session typing the command.
- **Operations checks.** URL, heartbeat, command, log and Monitoring Plugins checks for a project or a deploy target. A fault opens an item and starts an investigation, and the checks run right after each release.
- **A browser for each project.** A Chrome that you and your sessions share in the side panel.
- **A new look.** Quieter colors, fewer boxes, a chat like iMessage, and pages that work on a phone.
- **Removed:** the email notification channel and the planner role.

## Download

- **Mac with Apple silicon (M1 and later):** `MaximizePM-{VERSION}-arm64.dmg`
- **Mac with an Intel chip:** `MaximizePM-{VERSION}-x64.dmg`
- **Windows (x64, and Windows on Arm):** `MaximizePM-{VERSION}-x64-setup.exe`

To find your Mac's chip, open the Apple menu and choose **About This Mac**.

## Open the app on a Mac

Apple has not verified the app yet, because it is not notarized. The first time you open it, macOS shows this message: "Apple could not verify 'MaximizePM' is free of malware". To open the app:

1. Open the `.dmg` file, then drag **MaximizePM** to **Applications**.
2. Open **MaximizePM** from Applications. When the message appears, click **Done**. Do not click **Move to Trash**.
3. Open **System Settings**, then **Privacy & Security**.
4. Scroll down to **Security**. At the line "MaximizePM was blocked", click **Open Anyway**.
5. Enter your Mac password, then click **Open Anyway** again.

On macOS 14 and older: right-click the app in Applications, choose **Open**, then click **Open**.

macOS checks the app before each start while the app has its download mark. The app icon can bounce in the Dock for a minute or two before the window opens. To make the app open at once, remove the download mark. Open **Terminal** and enter this command:

```
xattr -dr com.apple.quarantine "/Applications/MaximizePM.app"
```

This command removes the macOS download check for this app only. Do it only for an app that you downloaded from this page.

If macOS says that the app "is damaged", you have an older copy. Move that app to the Trash and download this release.

## Open the app on Windows

The installer is not signed yet, so Windows SmartScreen shows "Windows protected your PC". To continue:

1. Click **More info**.
2. Click **Run anyway**.
