# MDRS GUI: Windows Front-End for mediafire_rs

A desktop GUI for [`mediafire_rs`](https://github.com/NicKoehler/mediafire_rs) (`mdrs`), the fast, async MediaFire folder downloader written in Rust. It adds a managed download queue, live speed graph, scheduling, post-download automation and more, so you don't have to drive the command line by hand.

> **Unofficial.** This project is not affiliated with or endorsed by the `mediafire_rs` author or by MediaFire. It only launches the `mdrs` executable you have installed. Use it in accordance with MediaFire's terms of service.

<!-- Add screenshots here, e.g. ![Queue tab](docs/queue.png) -->

---

## Features

### Queue
- Paste links, import `.txt` / `.md` / `.csv` files, or drag and drop text files onto the link box (links are extracted automatically; one per line, or separated by commas, semicolons or spaces).
- Tags (or **(No tag)**) with color-coded rows, and Low / Medium / High priority.
- Live search and filter, per-link pause / resume / remove (toolbar, right-click menu, double-click to toggle pause).
- Retry failed links, clear finished links, optional auto-clean of completed rows.
- Queue survives restarts.

### Statistics
- Real-time speed graph (with a **Low Resource Mode** that stops drawing it).
- Lifetime counters: total downloaded, average speed, links processed, downloaded today.
- Export metrics to CSV or HTML.

### Network
- Proxy list file, rotation every N completed links, and a standalone HTTP / SOCKS5 proxy with credentials (passed to `mdrs` with `-p` / `--proxy-download`).
- FlareSolverr URL with a connection test.
- Custom User-Agent and protected-folder password fields (see [Limitations](#limitations)).

### Settings
- **Concurrency:** connections per job (`-m`), parallel jobs, per-domain job limit, stall timeout, `mdrs` tries (`-t`), largest-first (`-r`).
- **Error handling:** auto-retry with selectable HTTP codes (502 / 503 / 504 / 429 / 404).
- **Files:** mirror MediaFire's folder structure or flatten, MD5 integrity check with a *delete* or *keep* policy, "Force Check" to re-verify history.
- **Automation:** auto-extract `.zip` / `.rar` / `.7z`, then do nothing, close the app, or sleep the PC when the queue finishes.
- **Disk check:** holds the queue and warns when free space falls below a threshold.
- **Global:** light / dark theme, close-button behavior (tray / quit / quit after queue), wake lock, daily data cap, completion sounds with volume, Quiet Mode and weekly speed-limit grids, profile export / import (JSON), update check, reset to defaults.
- Hover any option for a description.

### Log Console
- Raw `mdrs` output (stdout and stderr) in its own tab, with export to a text file.

---

## Installation

Works on Windows 10 / 11 (used on Windows 11). Follow the steps in order; skip any you already have.

### Step 1: Install Python

1. Download the latest Python installer for Windows from [python.org/downloads](https://www.python.org/downloads/).
2. Run it and **tick "Add python.exe to PATH"** on the first screen, then click **Install Now**.
3. Open a new terminal (Windows Terminal, PowerShell or Command Prompt) and check it worked:
   ```
   python --version
   ```

Python 3.9 or newer is required (the app is used with Python 3.14). The standard installer already includes Tkinter, which the GUI is built on.

### Step 2: Install Rust

`mdrs` is written in Rust, so you need the Rust toolchain to install it.

1. Download and run **`rustup-init.exe`** from [rustup.rs](https://rustup.rs).
2. If the installer says you need the **Microsoft C++ Build Tools** (Visual Studio Build Tools), let it install them, or install them first from the [Visual Studio downloads page](https://visualstudio.microsoft.com/downloads/) by choosing *Build Tools for Visual Studio* and the **Desktop development with C++** workload. This is needed to compile `mdrs`.
3. When prompted, press **Enter** to choose the default installation.
4. **Close and reopen your terminal**, then check it worked:
   ```
   cargo --version
   ```

### Step 3: Install mdrs

In your terminal, run:

```
cargo install mediafire_rs
```

This downloads and compiles `mdrs`, which can take a few minutes. When it finishes, check it worked:

```
mdrs --help
```

`cargo` places `mdrs.exe` in `%USERPROFILE%\.cargo\bin`, which the Rust installer adds to your PATH. To update `mdrs` later, run the same `cargo install mediafire_rs` command again.

### Step 4: Get MDRS GUI

1. Go to the [**latest release**](https://github.com/Farizma/mdrs-gui/releases/latest).
2. Under **Assets**, download **`mdrs_gui.pyw`** and save it anywhere you like, for example `C:\Tools\mdrs_gui\`.
3. **Double-click `mdrs_gui.pyw`** to start the app. It opens without a console window.

The app finds `mdrs` on your PATH or in `%USERPROFILE%\.cargo\bin`. If it lives elsewhere, set **Settings → Engine → mdrs executable**. If it isn't found at all, the app shows a warning on launch.

The app does not install anything for you, and settings are stored separately (see [Data location](#data-location)), so you can move or replace the file freely.

### Optional extras

Install these for extra features (the app works without them):

| Package / tool                 | Enables                                               |
|--------------------------------|-------------------------------------------------------|
| `tkinterdnd2`                  | Drag and drop of files onto the app                   |
| `pystray` + `pillow`           | System-tray mode and tray notifications               |
| [7-Zip](https://www.7-zip.org) | Extracting `.rar` and `.7z` (`.zip` works without it) |

```
pip install tkinterdnd2 pystray pillow
```

Restart the app afterwards. If a package fails to install on your Python version (for example one without a build for a very new release), the app still runs; only the feature that package provides is unavailable.

---

## Usage

1. Choose an **output folder** on the Queue tab.
2. Paste or drop MediaFire links and press **Add to queue**.
3. Press **Start queue**.

Tune speed and reliability under **Settings**; configure proxies under **Network**.

---

## How it works

`mdrs` itself only exposes `-i -o -m -t -r -p --proxy-download`. Everything else is implemented by the GUI around those flags:

| GUI feature          | Implementation                                                                       |
|----------------------|--------------------------------------------------------------------------------------|
| Queue and priorities | One `mdrs` process per link, started by the GUI's scheduler                          |
| Progress and speed   | Growth of each job's hidden staging folder (`<output>/.mdrs_stage/`)                 |
| Pause / resume       | The `mdrs` process is suspended / resumed                                            |
| Speed-limit schedule | Brief duty-cycle pauses of `mdrs` (approximate)                                      |
| Stall timeout        | Watchdog kills a link that stops making progress                                     |
| Mirror / flatten     | Files are moved from staging to the output folder after completion                   |
| MD5 / integrity      | Checks for empty or corrupt `.zip` files and records MD5 hashes for later comparison |
| Extraction           | Python `zipfile`; `7z` for `.rar` / `.7z`                                            |

## Data location

Settings and state are stored in `%APPDATA%\MdrsGUI\`:

`settings.json`, `queue.json`, `stats.json`, `history.json`, `proxies_active.txt`

Deleting the folder resets the app. Exported profiles contain all settings except the link-box text and window size.

---

## Limitations

- **FlareSolverr, custom User-Agent and folder password** are saved and (for FlareSolverr) connection-tested, but `mdrs` has no option to use them yet.
- **Speed limiting** works by pausing the process, so actual speed varies around the limit.
- **MD5 check** cannot compare against MediaFire's original hash. It detects empty or broken files and later changes to files the app downloaded.
- **Disk check** compares free space to a threshold you set, because the total download size isn't known up front.
- **Proxy rotation** assumes one proxy URL per line (e.g. `http://user:pass@host:port`) and applies to links started after the rotation.
- **Volume** applies to built-in melodies and `.wav` sounds; the `System:` sounds follow the Windows volume.
- Per-link controls are in the toolbar, right-click menu and double-click, not as icons inside each row.

## Troubleshooting

| Problem                                   | Fix                                                                                     |
|-------------------------------------------|-----------------------------------------------------------------------------------------|
| `python` is not recognized                | Reinstall Python and tick **Add python.exe to PATH**, then open a new terminal.         |
| `cargo` is not recognized                 | Close and reopen your terminal after installing Rust.                                   |
| `cargo install` fails with a linker error | Install the **Desktop development with C++** workload from Visual Studio Build Tools.   |
| "mdrs was not found" on launch            | Finish Step 3, or set the path in Settings → Engine.                                    |
| Double-clicking `mdrs_gui.pyw` does nothing | Right-click → **Open with** → Python; or run `python mdrs_gui.pyw` to see any errors. |
| Drag and drop does nothing                | `pip install tkinterdnd2` and restart.                                                  |
| Close button doesn't go to the tray       | `pip install pystray pillow`; otherwise it minimizes to the taskbar.                    |
| `.rar` / `.7z` not extracted              | Install 7-Zip (see the log tab for the message).                                        |
| Many links failing with 429 / 502         | Lower connections per job and parallel jobs; consider a proxy.                          |

## Contributing

Issues and pull requests are welcome. The whole application is a single file, `mdrs_gui.pyw`, built with the Python standard library (plus the optional packages above).

## Credits

- [`mediafire_rs`](https://github.com/NicKoehler/mediafire_rs) by NicKoehler: the downloader this GUI wraps.
