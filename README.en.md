# `TuneFree`

**Turn the encrypted songs you downloaded in QQ Music (`.mflac` / `.mgg`) into `MP3` or `FLAC` that any player can open.**

No membership tricks, no uploading — everything happens on your own computer.

[⬇️ Download latest](https://github.com/zksbx/TuneFree/releases/latest) · [🌐 Website & help](https://zksbx.github.io/TuneFree/tunefree.en.html) · [☁️ Lanzou mirror](https://wwbtj.lanzouc.com/b01d7csi8j) (access code `0211`) · 💬 QQ group: 1067975918 · [中文 README](README.md)

---

## What problem does it solve

Some tracks downloaded in QQ Music come in encrypted containers:

| Extension | What it really is | Typical case |
|---|---|---|
| `.mflac` | encrypted `FLAC` (lossless) | lossless downloads |
| `.mgg` | encrypted `OGG` | some licensed tracks |

Only QQ Music can play these. Copy one to a car stereo, an MP3 player or any other player and all you get is "unsupported format".

TuneFree does something simple: **it calls QQ Music's own decryption function inside the running process**, restores the file to a normal `FLAC`/`OGG`, then converts it to `MP3` with `FFmpeg`.

```
.mflac / .mgg  ──decrypt──▶  .flac / .ogg  ──encode──▶  .mp3
```

## Three steps

1. **Open QQ Music and log in**, keep it running (you don't need to play anything)
2. Double-click `TuneFree.exe`, pick the folder holding your `.mflac` or `.mgg` files
3. Press **Start**, wait for the progress bar, collect the `MP3`s from the output folder

Output files keep the original names, so you can copy them straight onto a phone or car stereo.

## Support the author

`TuneFree` is completely free — no ads, no feature gates, no paid tier. If it genuinely helped you, you can buy the author a milk tea:

<p align="center">
  <img src="docs/support.png" alt="Left: WeChat tip QR, right: Alipay tip QR" width="560">
</p>

No money is fine too — [leave a star](https://github.com/zksbx/TuneFree) or tell someone who needs it. That helps just as much.

## Is it a virus? Read this first

The previous release **v2.0.0** shipped on **2026-06-30** and had been downloaded **3,660 times** on Lanzou by **2026-10-31** (the counter comes from Lanzou's own backend — the author cannot edit it). Thousands of people have installed and used it; something genuinely malicious would not stay quiet that long.

<p align="center">
  <a href="docs/downloads.png"><img src="docs/downloads.png" alt="Lanzou backend screenshot (2026-10-31): TuneFree-2.0.0.exe, 3,660 downloads" width="760"></a>
</p>

Your antivirus will still complain, and that is expected: TuneFree injects code into the QQ Music process with Frida, which is exactly what malware does. The difference is that the entire source is public — you can read every line of what it does.

## Features

- 🎵 Batch decryption of `.mflac` / `.mgg`, walks subfolders automatically
- 🎚 Three output modes: MP3 only, lossless FLAC only, or keep both
- ⚡ Resume friendly: a run that was interrupted skips finished files next time
- 📊 Live progress bar — you can see which step is stuck
- 🔔 Built-in update check
- 🔒 Offline: apart from the update check it contacts no server at all

## Requirements

- Windows 10 / 11 (64-bit)
- QQ Music client (logged in — decryption borrows its key)

## FAQ

<details>
<summary><b>"Windows protected your PC" — a blue window blocks it from running</b></summary>

That is SmartScreen's "unknown publisher" warning, not a virus detection — any `exe` without a paid code-signing certificate is stopped the first time.

Two ways around it, pick either:

1. **Before extracting (recommended)**: right-click the zip → Properties → at the bottom of the General tab tick **Unblock** → OK, then extract. Files extracted this way are no longer tagged as "from the internet" and will never trigger the warning.
2. **Already extracted**: in the blue window click **More info**, then **Run anyway** at the bottom right. Once per file is enough.

The zip also contains `!首次运行请看.txt`. That file is only visible after extracting, so it only documents method 2 (plus how to add an antivirus exclusion); method 1 has to be done before extracting.
</details>

<details>
<summary><b>My antivirus flags it as a virus</b></summary>

The app injects code into the QQ Music process with Frida — that behaviour looks exactly like malware, so almost every antivirus reports it.

Fix: add the `TuneFree` folder to your antivirus exclusions. The source is fully public, so you can check every line yourself.
</details>

<details>
<summary><b>"Please make sure the QQ Music client is running, then try again"</b></summary>

Start QQ Music and log in first, then press Start. Decryption depends on the key living inside that process — no process, no key.
</details>

<details>
<summary><b><code>invalid size</code> or "QQ Music version mismatch"</b></summary>

A QQ Music update renamed its internal functions, so the current build can't be injected.

- Roll QQ Music back to a build known to work, or
- Wait for TuneFree to support the new version (watch Releases)
</details>

<details>
<summary><b>Decryption works, but every MP3 conversion fails</b></summary>

Usually your security software is blocking `ffmpeg.exe` from creating files. The app automatically retries by piping the output and writing the file itself, which normally gets through.

If it still fails, add the `TuneFree` folder to your exclusions and try again.
</details>

<details>
<summary><b>A handful of songs simply won't convert</b></summary>

Some tracks need to be played once in QQ Music first, so the decryption is loaded, and then run TuneFree.
</details>

<details>
<summary><b>Mac / Linux support?</b></summary>

Windows only for now. The core logic doesn't depend on Windows, but the injection target — QQ Music — only exists on Windows.
</details>

## Run from source

```bash
# 1. install dependencies
pip install -r requirements.txt

# 2. put ffmpeg.exe in this folder (the app looks for it here)
#    Windows builds: https://www.gyan.dev/ffmpeg/builds/ (pick release essentials)

# 3. launch the GUI
python app.py

# or use the command line (app.py and main.py both work)
python app.py --cli -i "D:\downloads" -o "D:\music" -b 320k
python main.py -i "D:\downloads" -o "D:\music" -b 320k
```

Command-line options:

| Option | Description |
|---|---|
| `-i, --input` | input folder, defaults to the current folder |
| `-o, --output` | output folder, defaults to `./output` |
| `--mp3-dir` / `--flac-dir` | set the `MP3` / `FLAC` output locations separately |
| `-b, --bitrate` | `MP3` bitrate, default `192k` |
| `--keep-flac` | keep the decrypted `FLAC` |
| `--no-mp3` | output `FLAC` only, no `MP3` |
| `--force` | rebuild `MP3`s that already exist |
| `-v, --version` | print the version |

## Project layout

```
app.py               single entry point: GUI with no args, CLI with --cli
TuneFree.py          GUI (PySide6)
main.py              CLI / backend: inject, decrypt, encode
hook_qq_music.js     Frida injection script (the decryption core)
version.py           single source of truth for the version and repo URL
version.json         update source for mainland users (served via jsDelivr)
TuneFree.spec        PyInstaller spec (produces exactly one TuneFree.exe)
docs/                website (static single page)
发版清单.md           release checklist (Chinese)
```

## Disclaimer

This tool is for **personal study and interoperability** only, used to decrypt music files you legitimately downloaded on this machine so you can play them on devices you own.

Do not redistribute, re-share or use it commercially. Any legal consequences are the user's own.

## License

The code is released under the [MIT License](LICENSE). Fork it, change it, build on it.

One reminder: MIT covers the **code**. It does not loosen the limits on **how you may use it** described in the disclaimer above.

## Credits

- [Frida](https://frida.re/) — process injection
- [FFmpeg](https://ffmpeg.org/) — audio encoding
- [music-decryptor](https://github.com/ericjuice/music-decryptor) — where the decryption script started
- [Unlock Music](https://github.com/unlock-music/unlock-music) — format parsing experience
- [PyInstaller](https://pyinstaller.org/) — packaging

## Support

- 🐛 [Open an issue](https://github.com/zksbx/TuneFree/issues)
- 💬 QQ group: 1067975918
- ☕ [Afdian](https://ifdian.net/a/TuneFree)
