# Burnboard

A small macOS app that shows your Claude Code usage: tokens, cost at API prices, and the limits of your Claude plan. It reads the Claude Code logs on your Mac (and the other places listed under "What it reads"), asks your own Claude Code who is signed in, and downloads a public price list. It has no server and no account of its own. "No telemetry" covers Burnboard's own code; Claude Code itself, which Burnboard starts for the checks below, follows your own Claude Code settings. Nothing runs while the window sits idle.

Burnboard is an independent tool and is not affiliated with Anthropic.

## What it needs

- macOS 12 or later.
- Nothing else is required. Burnboard shows whatever Claude usage records it finds on the Mac: Claude Code logs, Cowork sessions, and the plan usage history the Claude desktop app keeps. Live plan limits need Claude Code installed and signed in. Whenever the app opens, and on the Setup screen, it starts your own `claude --version` and `claude auth status --json` to see whether Claude Code is there and signed in. The sign-in itself is done by `claude` in Anthropic's own flow in your browser, and the app never sees your password or token. Without a sign-in the dashboard still works from the logs.
- Python 3 from Apple's command line tools. (Python 3 from Homebrew also works.)

The first screen of the app checks these and offers one button for each missing item: "Install Apple's command line tools" (starts Apple's own installer) and "Sign in to Claude Code" (runs your own `claude auth login`, which opens Anthropic's sign-in page in your browser; the window updates when you are done). Confirm in the window that opens, then click "Check again" where the screen asks for it.

## Install

From the download (`Burnboard-<version>.dmg`): open it and drag Burnboard onto the Applications folder shown next to it, then open Burnboard from Applications. The app is not signed with an Apple Developer ID, so macOS blocks it the first time:

1. Double-click Burnboard. macOS says it cannot verify the developer. Click Done.
2. Open System Settings, then Privacy & Security, and scroll to the Security section. Next to the message that "Burnboard" was blocked to protect your Mac, click Open Anyway.
3. Confirm with Touch ID or your password. If macOS asks once more, click Open Anyway again.

After that it opens normally. (The labels "Privacy & Security", "Security" and "Open Anyway" were checked in the system files of macOS 26; the Touch ID prompt and the second confirmation are described from Apple's usual flow and were not checked.)

From the source: run `./install.sh`. It needs the Xcode command line tools, builds the app and installs it to /Applications. An app built on your own Mac is not blocked, so no Open Anyway step. `native/package.sh` makes `dist/Burnboard-<version>.dmg` (the download) and `dist/Burnboard-<version>.zip` (what the updater fetches).

## What it reads

- The Claude Code logs of the Claude folder you pick: `projects/**/*.jsonl` and the `.meta.json` files next to them. It only reads them. The default folder is `~/.claude`, or the folder in `CLAUDE_CONFIG_DIR` when the app itself has that variable (an app started from the Dock or Finder does not see variables you set only in your shell profile). You can add other folders.
- For the default folder only, also: the other default place of Claude Code logs, `~/.config/claude/projects`, when it exists, and the Cowork sessions of the Claude desktop app, `~/Library/Application Support/Claude/local-agent-mode-sessions/**/*.jsonl`, when present. Links inside all of these folders (also inside `projects/`) are not followed. The first screen shows what was found, with file counts.
- For the default folder only, also: the plan usage history that the Claude desktop app keeps, `~/Library/Application Support/Claude/plan-usage-history.json`, when present. It only reads it: a regular file of at most 1 MB, from the last 15 days, and only the times and the two percentages (5-hour and 7-day). The organisation id in that file is never kept, shown or written anywhere. Burnboard shows these readings as "recorded by the Claude app" when the limits are not read from Claude Code, and as a small chart.
- Whether Claude Code is signed in, the e-mail and the plan, from `claude auth status --json`. They are shown on the first screen and not stored.

## What leaves the Mac

The window loads its page from inside the app (`burnboard://app/`), and its security policy only allows the page to talk to itself. These are the only network requests:

- The app's own code downloads a public price list from `raw.githubusercontent.com` during a refresh and works out the costs itself. It is a plain download; no data from your Mac is in it. A copy of the list ships inside the app and is used when the download fails.
- The app's own code downloads a small version file, `release.json`, from `raw.githubusercontent.com` during a refresh, to see whether a newer Burnboard exists. Also a plain download with nothing from your Mac in it; only the version number in it reaches the page. It is skipped when the app does not know its own version (development), and a failed download is simply ignored.
- The Update button, only when you click it: the app downloads the new version's zip from `github.com/baraba04/burnboard/releases` (GitHub may hand the file over from `objects.githubusercontent.com` or `release-assets.githubusercontent.com`; no other host is followed). Before anything is replaced it checks an Ed25519 signature on the release file against a key built into the app, the exact download address, the size and the SHA-256 of the zip, that the zip holds one Burnboard app of the same bundle id and the signed version, and that its code signature is intact. If any check fails the installed app stays as it is. It then replaces itself in its own place (or, when it runs from a place it cannot write to, in /Applications, else in ~/Applications), starts the new copy and quits. Nothing is sent: it is a plain download.
- `claude` (your own Claude Code), once per refresh, only if you leave the limits switch on: one tiny call to the Haiku model with a fixed prompt, on your own subscription, to read the plan limit percentages. Your own Claude Code makes the call, with settings sources, tools, MCP servers and slash commands switched off, and it may include your own Claude Code instruction files, as it does for any session. No log content or usage data is in it. On the developer's Mac the call measured about 830 input tokens in total. Every `ANTHROPIC_*` variable (API key, auth token, base URL) and the `CLAUDE_CODE_USE_*` variables are removed from its environment, so it goes to Anthropic on the subscription and not to a key or host the app may have inherited. Switch it off on the first screen (Setup button at the top) and the app makes no call on your subscription.
- The "Sign in to Claude Code" button, only when you click it: the app starts `claude auth login` and Claude Code itself opens Anthropic's sign-in page in your browser and stores the sign-in. The app reads nothing it prints and never handles a password, code or token. If Claude Code cannot start the sign-in without a terminal, the app opens the same command in Terminal instead.

Burnboard never uploads your logs or your usage numbers. What your own Claude Code does when Burnboard starts it (`--version`, `auth status`, the limits call, `auth login`) follows your own Claude Code settings.

## Where it keeps its own files

- `~/Library/Application Support/usage-claude/`: your choices (config.json: consent, Claude folders), the archive of past days and the history of limit readings. Kept for good. The archive holds project names and working-folder paths. If an archive file is damaged, the unusable days are ignored and a copy `archive.json.corrupt-<time>` is kept next to it before it is rewritten.
- `~/Library/Caches/claude-usage-dashboard/`: the last snapshot and parsed logs (derived from your logs, so they include project names, working-folder paths and session titles), and the last downloaded price list. Safe to delete. It can also hold `sign-in.command`, the two-line script that the sign-in button opens in Terminal when the sign-in in the browser cannot be started.
- The language choice (WebKit storage of the app) and the window position (the app's preferences file).

Extra Claude folders you add keep their own files in a `profiles/` folder inside the two folders above.

## Remove everything

Quit the app, then delete:

- `/Applications/Burnboard.app`
- `~/Library/Application Support/usage-claude/`
- `~/Library/Caches/claude-usage-dashboard/`
- `~/Library/WebKit/app.burnboard.mac/`, `~/Library/Caches/app.burnboard.mac/` and `~/Library/Preferences/app.burnboard.mac.plist` (language, web view cache and window position)

## Development

`app/serve.py` is a development handler for testing the page in a browser: it answers one HTTP request on stdin and stdout, so it runs under launchd or another inetd-style launcher bound to 127.0.0.1. The distributed app does not use it and does not contain it. Tests: `python3 tests/test_cli.py`, `tests/test_parse.py`, `tests/test_archive.py`, `tests/test_pricing.py`. `python3 tests/compare_ccusage.py` is a developer check against ccusage (it downloads ccusage from npm); it is not part of the app.

`native/package.sh` also writes a `.sha256` file next to the dmg and the zip, and `dist/release.json`, the signed release file that the app's update check reads. To check a download, run `shasum -a 256 -c Burnboard-<version>.dmg.sha256` in the folder that holds both files.

Force-quitting the app (not a normal quit) can leave a helper running until its own time limit: a refresh 180 seconds, a sign-in 300 seconds, the version and sign-in check about 10 seconds.

## License

Copyright (c) 2026 Burnboard. All rights reserved. The source is published so that anyone can check what the app does. No permission is given to copy, modify, redistribute or sell it.

Burnboard is not affiliated with or endorsed by Anthropic.
