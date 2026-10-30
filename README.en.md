# TuneFree

**Convert QQ Music's encrypted downloads (`.mflac` / `.mgg`) into plain MP3 or FLAC — locally, offline, no account tricks.**

[⬇️ Download](https://github.com/zksbx/TuneFree/releases/latest) · [🌐 Website](https://zksbx.github.io/TuneFree/) · [Chinese README](README.md)

---

## What it does

QQ Music stores some downloads in encrypted containers:

| Extension | Real content |
|---|---|
| `.mflac` | Encrypted FLAC (lossless) |
| `.mgg` | Encrypted OGG |

Only QQ Music can play them. Copy one to a car stereo or a normal player and you get "unsupported format".

TuneFree calls QQ Music's **own decryption function inside its process** (via Frida), restores the file to a normal FLAC/OGG, then re-encodes it to MP3 with FFmpeg.

```
.mflac / .mgg  ──decrypt──▶  .flac / .ogg  ──encode──▶  .mp3
```

## Usage

1. Start QQ Music and log in (keep it running, you don't need to play anything)
2. Run `TuneFree.exe` and pick the folder containing your `.mflac` files
3. Hit **Start**, wait for the progress bar, grab your MP3s from the output folder

## Features

- Batch decryption, walks subfolders
- Output MP3 only, FLAC only, or both
- Resume-friendly: already-finished files are skipped on the next run
- Live progress bar
- Built-in update check
- Everything runs locally; nothing is uploaded

## Requirements

- Windows 10 / 11 (64-bit)
- QQ Music client running

## FAQ

<details>
<summary><b>My antivirus flags it</b></summary>

The app injects code into the QQ Music process with Frida, which looks exactly like malware behaviour.

Add the TuneFree folder to your antivirus exclusions. The source is fully public.
</details>

<details>
<summary><b>"Please make sure QQ Music is running"</b></summary>

Start QQ Music and log in first — the decryption key lives inside that process.
</details>

<details>
<summary><b><code>invalid size</code> / "version mismatch"</b></summary>

A QQ Music update changed internal symbol names. Downgrade QQ Music to a supported build, or wait for a TuneFree update.
</details>

<details>
<summary><b>Decryption works but MP3 conversion fails</b></summary>

Security software is usually blocking `ffmpeg.exe` from creating files. TuneFree automatically retries in "pipe to stdout, write from Python" mode. If it still fails, whitelist the folder.
</details>

## Run from source

```bash
pip install -r requirements.txt
# put ffmpeg.exe next to main.py
python TuneFree.py
# or CLI:
python main.py -i "D:\downloads" -o "D:\music" -b 320k
```

## Disclaimer

For personal study and interoperability only, on files you legitimately downloaded. Do not redistribute or use commercially. You are responsible for how you use it.

## Credits

Frida · FFmpeg · [music-decryptor](https://github.com/ericjuice/music-decryptor) · [Unlock Music](https://github.com/unlock-music/unlock-music) · PyInstaller
