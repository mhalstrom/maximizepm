MaximizePM desktop app {TAG}. The app carries its own Python, so you do not have to install anything else.

## What's new in {TAG}

Everything below is new since v0.1.8, the last download (October 2).

- **Your agents are chats.** Sessions is now the first page, and each agent is a chat that you can read and type into.
- **Stop an agent mid-turn.** While an agent works, a Stop button sits where Send was. If you type while it works, your message goes in when the turn ends. If you want to cut in at once, press Send now.
- **More agents.** Besides Claude Code: Codex, Gemini CLI, Copilot CLI, Cursor, Grok, OpenCode, Pi, Hermes, Aider, Goose and Amp.
- **About twice as fast.** Commands and the page respond in about half the time. On a phone, the Sessions tab opens almost at once.
- **Pages you can comment on.** An agent can show you a plan or a draft as a page, and you comment right on it.
- **A manager that handles the small stuff.** It answers what it can and brings you only what needs you.
- **It watches your app after each release.** Point it at your site, your logs or your pages. After each release it checks them, and when something breaks it opens a task with the error and where it came from. The agent proves the cause before it changes any code.
- **It keeps itself running.** A small background job restarts MaximizePM if it stops, and tells you if it can't.
- **Tasks that come back.** Taxes, renewals, an annual report: add one once, and the task shows up each time it's due.
- **A new look** that works on a phone.
- **Windows:** the Windows app is gone. Run MaximizePM inside WSL 2 instead.

## Download

- **Mac with Apple silicon (M1 and later):** `MaximizePM-{VERSION}-arm64.dmg`
- **Mac with an Intel chip:** `MaximizePM-{VERSION}-x64.dmg`
- **Windows:** there's no Windows app. Install WSL 2 and run MaximizePM there: see [Windows](https://github.com/mhalstrom/maximizepm#windows) in the README.

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
