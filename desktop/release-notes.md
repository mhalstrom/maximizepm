MaximizePM desktop app {TAG}. The app carries its own Python, so you do not have to install anything else.

## MaximizePM {TAG}: latest stable release

MaximizePM runs your coding agents as a team. You say what to build. It keeps the work in one queue, gives each agent the next task, and brings you only what needs you.

- **Your agents are chats.** Each agent is a chat that you can read and type into. Stop it mid-turn, or send a message that goes in when its turn ends.
- **Works with the agents you use.** Claude Code, Codex, Gemini CLI, Copilot CLI, Cursor, Grok, OpenCode, Pi, Hermes, Aider, Goose and Amp.
- **One queue for all the work.** Tasks, goals and what waits on what. Each agent takes the most important task that's ready.
- **A manager that handles the small stuff.** It answers what it can and brings you only what needs you.
- **Pages you can comment on.** An agent shows you a plan or a draft as a page, and you comment right on it.
- **Releases that check themselves.** Each release gets a review before it ships. After it ships, MaximizePM checks your site, your logs and your pages. When something breaks, it opens a task with the error and where it came from.
- **It keeps itself running.** A small background job restarts MaximizePM if it stops, and tells you if it can't.
- **Tasks that come back.** Taxes, renewals, an annual report: add one once, and it shows up each time it's due.
- **Works on your phone.**
- **Windows:** run MaximizePM inside WSL 2.

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
