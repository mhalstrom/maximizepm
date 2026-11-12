Fixes: when you start MaximizePM from the app, its checks and hooks find the same commands as in your terminal.

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
