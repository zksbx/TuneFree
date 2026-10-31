# `TuneFree`

**Turn the encrypted songs you downloaded in QQ Music (`.mflac` / `.mgg`) into `MP3` or `FLAC` that any player can open.**

[![Release](https://img.shields.io/github/v/release/zksbx/TuneFree?color=2ea043&label=release)](https://github.com/zksbx/TuneFree/releases/latest)
[![Downloads](https://img.shields.io/badge/downloads-3.6k%2B-3b82f6)](https://wwbtj.lanzouc.com/b01d7csi8j)
[![Stars](https://img.shields.io/github/stars/zksbx/TuneFree?color=e3b341)](https://github.com/zksbx/TuneFree/stargazers)
[![License](https://img.shields.io/github/license/zksbx/TuneFree?color=8957e5)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-6e7781)](README.en.md#requirements)

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
- **Only the `Standard` / `HQ` / `SQ` quality tiers are supported.** Tracks downloaded as Hi-Res (臻品音质), Master (臻品母带) or Dolby Atmos are not supported and will fail to decrypt.

## FAQ

### "Windows protected your PC" — a blue window blocks it from running

That is SmartScreen's "unknown publisher" warning, not a virus detection — an unsigned exe is blocked the first time, always.

Two ways out:

1. **Before unzipping (recommended)**: right-click the zip → Properties → tick **Unblock** at the bottom of the General tab → Apply, then unzip. The extracted files are no longer marked as "from the internet" and will never prompt again.
2. **Already unzipped**: in the blue window click **More info**, then **Run anyway** at the bottom right. Once per file is enough.

The zip also ships a `!首次运行请看.txt`, but that file is only visible after unzipping, so it documents method 2 only (plus how to whitelist the app when antivirus blocks it).

### My antivirus flags it as a virus

The app uses Frida to inject code into the QQ Music process — behaviour that looks exactly like malware, so most antivirus products will report it.

Fix: add the TuneFree folder to your antivirus trust list. The source is fully public, you can read every line.

### "Please make sure the QQ Music client is running, then try again"

Open QQ Music and log in first, then hit start. Decryption borrows the key from the QQ Music process; without it there is no key.

### `invalid size` or "QQ Music version mismatch"

QQ Music changed its internal function names, so the current build cannot inject into it.

- Downgrade QQ Music to a known-good version, or
- Wait for TuneFree to support the new release (watch Releases)

### Decryption works, but every MP3 conversion fails

Usually security software is preventing `ffmpeg.exe` from creating files. The app automatically retries through "pipe output + write the file itself", which normally gets through.

If it still fails, add the TuneFree folder to your trust list.

### A handful of songs simply won't convert

Check the quality tier first. Only **Standard / HQ / SQ** are supported; if a song was downloaded as **Hi-Res (臻品音质), Master (臻品母带) or Dolby Atmos** it carries no normal audio stream, so conversion always fails — re-download it at SQ or lower and retry.

If it is already Standard / HQ / SQ, some files need to be played once in QQ Music first so the full data lands in the local cache, then convert them.

### Mac / Linux support?

Windows only for now. The core logic is platform-independent, but the QQ Music client it injects into exists only on Windows.

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
