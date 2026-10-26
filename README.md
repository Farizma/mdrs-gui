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

## Requirements

| Requirement | Notes |
|---|---|
| Windows 10 / 11 | Pause, wake lock, sounds and sleep use Windows APIs. Used on Windows 11. |
| Python 3.9+ | The python.org installer includes Tkinter. Used with Python 3.14. |
| `mdrs` | `cargo install mediafire_rs` (needs [Rust](https://rustup.rs)). |

**Optional**

| Package / tool | Enables |
|---|---|
| `tkinterdnd2` | Drag and drop of files onto the app |
| `pystray` + `pillow` | System-tray mode and tray notifications |
| [7-Zip](https://www.7-zip.org) | Extracting `.rar` and `.7z` (`.zip` works without it) |

## Installation

```bash
git clone https://github.com/Farizma/mdrs-gui.git
cd mdrs-gui

# optional extras
pip install tkinterdnd2 pystray pillow

python mdrs_gui.pyw
```

Because the file uses the `.pyw` extension, you can also just double-click `mdrs_gui.pyw` and it opens without a console window.

The app finds `mdrs` on your `PATH` or in `%USERPROFILE%\.cargo\bin`. If it lives elsewhere, set **Settings → Engine → mdrs executable**.

If an optional package fails to install on your Python version (for example a package without a wheel for a very new release), the app still runs; only the feature that package provides is unavailable.

There is nothing else to build, and the app does not install anything for you.

## Usage

1. Choose an **output folder** on the Queue tab.
2. Paste or drop MediaFire links and press **Add to queue**.
3. Press **Start queue**.

Tune speed and reliability under **Settings**; configure proxies under **Network**.

---

## How it works

`mdrs` itself only exposes `-i -o -m -t -r -p --proxy-download`. Everything else is implemented by the GUI around those flags:

| GUI feature | Implementation |
|---|---|
| Queue and priorities | One `mdrs` process per link, started by the GUI's scheduler |
| Progress and speed | Growth of each job's hidden staging folder (`<output>/.mdrs_stage/`) |
| Pause / resume | The `mdrs` process is suspended / resumed |
| Speed-limit schedule | Brief duty-cycle pauses of `mdrs` (approximate) |
| Stall timeout | Watchdog kills a link that stops making progress |
| Mirror / flatten | Files are moved from staging to the output folder after completion |
| MD5 / integrity | Checks for empty or corrupt `.zip` files and records MD5 hashes for later comparison |
| Extraction | Python `zipfile`; `7z` for `.rar` / `.7z` |

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

| Problem | Fix |
|---|---|
| "mdrs was not found" on launch | Run `cargo install mediafire_rs`, or set the path in Settings. |
| Drag and drop does nothing | `pip install tkinterdnd2` and restart. |
| Close button doesn't go to the tray | `pip install pystray pillow`; otherwise it minimizes to the taskbar. |
| `.rar` / `.7z` not extracted | Install 7-Zip (see the log tab for the message). |
| Many links failing with 429 / 502 | Lower connections per job and parallel jobs; consider a proxy. |

## Contributing

Issues and pull requests are welcome. The whole application is a single file, `mdrs_gui.pyw`, built with the Python standard library (plus the optional packages above).

## Credits

- [`mediafire_rs`](https://github.com/NicKoehler/mediafire_rs) by NicKoehler: the downloader this GUI wraps.

## License

Released under the [MIT License](LICENSE).
