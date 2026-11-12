MaximizePM desktop app {TAG}. The app carries its own Python, so you do not have to install anything else.

## What's new in {TAG}

About 50 changes since v0.2.0. The main ones:

- **Stop and Send now.** Stop, in a session's header or its ⋯ menu, ends the turn that's running, just like the key you'd press in its terminal. The session keeps its context and its task. Send now (the bolt next to the send arrow, or Ctrl+Enter or Cmd+Enter) stops the turn and sends your message right away, instead of queuing it. In Codex, a second Stop also ends the commands the turn left running in the background. `maxpm interrupt` does the same from the command line.
- **Faster everywhere.** `maxpm` commands now run inside `maxpm serve`, which keeps the queue open between commands: `maxpm inbox` went from 87 ms to 43, and `maxpm show` from 221 ms to 117. The page answers a click or a tab switch in under 100 ms. On a phone, the Sessions tab went from 474 ms to 10.
- **A watchdog.** A small job runs every minute even when `maxpm serve` doesn't (`maxpm serve --install-watchdog`). It runs the checks marked outside, restarts serve when it's down, and sends you a notice when it can't fix it. On Linux, the login item and the watchdog are systemd user units.
- **MaximizePM watches itself.** A profile ships with nine checks of MaximizePM: the page, the relay, stuck sessions, failed notices and errors in its logs. `maxpm ops setup` offers it first.
- **More checks.** A visual check takes screenshots of a page at each width and theme and compares them with the approved ones. A log check reads only the new lines of a log, here or over ssh. A check can run on a schedule (`--cron "0 3 * * *"`), and each check says what kind of failure counts as a fault.
- **Release watch.** A deploy target can run its checks at set times after each release (`maxpm target watch <target> 1m 10m 1h 24h`). After two released fixes that didn't clear a fault, no more agents start on it: the manager decides the next step.
- **Faults come with their facts.** Before an agent starts on a fault, MaximizePM gives it a kind, the errors grouped and counted, the code location and the release it came after. The agent states the cause with a proof that MaximizePM runs, before any fix. A fault right after a release proposes the rollback first, and only a person starts it. Noise rules keep known noise from opening items.
- **The system description.** Each project can describe its code, its infrastructure and its rules in one place (`maxpm ops system`). Faults read it. Agents can propose changes, and you or the manager accept them.
- **Scheduled tasks.** Repeating obligations like an annual report, taxes or a renewal: `maxpm schedule add` adds the task each time it comes due, with the lead time you pick.
- **Smaller fixes.** With no time zone set, times follow your computer's daylight saving rules. `maxpm done --ship` refuses before it closes an item that can't ship. A review pins the commit it checked, so the release ships exactly that. `maxpm done --changed` keeps one plain line about what changed, for you. Tooltips name what a button does in a few words.

### Also new since v0.1.8 (v0.2.0 had no download)

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
