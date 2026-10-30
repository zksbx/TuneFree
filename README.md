# TuneFree

**把 QQ音乐下载的加密歌曲（`.mflac` / `.mgg`）一键变成任何播放器都能放的 MP3 或 FLAC。**

不需要会员，不需要上传，全程在你自己的电脑上完成。

[⬇️ 下载最新版](https://github.com/zksbx/TuneFree/releases/latest) · [🌐 官网与使用帮助](https://zksbx.github.io/TuneFree/) · 💬 QQ 群：1067975918

---

## 它解决什么问题

在 QQ音乐里下载的部分歌曲是加密格式：

| 后缀 | 实际内容 | 常见场景 |
|---|---|---|
| `.mflac` | 加密的 FLAC（无损） | 无损音质下载 |
| `.mgg` | 加密的 OGG | 部分版权歌曲 |

这些文件只有 QQ音乐能放，拷到车机、MP3、其他播放器里全是「格式不支持」。

TuneFree 做的事很简单：**在 QQ音乐进程内调用它自己的解密函数**，把文件还原成正常的 FLAC/OGG，再用 FFmpeg 转成 MP3。

```
.mflac / .mgg  ──解密──▶  .flac / .ogg  ──转码──▶  .mp3
```

## 三步就能用

1. **打开 QQ音乐并登录**，保持运行（不用播放歌曲）
2. 双击 `TuneFree.exe`，选择存放 `.mflac` 文件的目录
3. 点「开始执行」，等进度跑完，去输出目录拿 MP3

转换出来的文件名和原曲一致，可以直接拷进手机或车机。

## 功能

- 🎵 支持 `.mflac` / `.mgg` 批量解密，自动递归子文件夹
- 🎚 三种输出：只要 MP3、只要无损 FLAC、或者两个都留
- ⚡ 断点续跑：中途关掉，重跑会跳过已经完成的文件
- 📊 实时进度条，卡在哪一步一目了然
- 🔔 自动检查更新
- 🔒 不联网、不上传，除了检查更新不会连接任何服务器

## 系统要求

- Windows 10 / 11 64 位
- QQ音乐客户端（需要登录，解密靠它提供密钥）

## 常见问题

<details>
<summary><b>杀毒软件报毒怎么办？</b></summary>

程序用 Frida 往 QQ音乐进程里注入代码——这个行为本身就跟病毒一模一样，所以杀软基本都会报。

解决办法：把 TuneFree 目录加入杀软信任区。源码完全公开，不放心可以自行看每一行在做什么。
</details>

<details>
<summary><b>提示「请确保 QQ音乐客户端正在运行后重试」</b></summary>

先把 QQ音乐打开并登录，再点开始。解密依赖 QQ音乐进程里的密钥，它没运行就没有密钥可用。
</details>

<details>
<summary><b>解密报 <code>invalid size</code> 或「QQ音乐版本不匹配」</b></summary>

QQ音乐更新后改了内部函数名，当前版本注入不进去。

- 把 QQ音乐降级到已知可用的版本，或者
- 等 TuneFree 适配新版本（关注 Release）
</details>

<details>
<summary><b>解密成功，但转 MP3 全部失败</b></summary>

多数是安全软件禁止 `ffmpeg.exe` 创建文件。程序会自动改用「管道输出 + 由程序落盘」的方式重试，通常能过。

如果还是失败，把 TuneFree 目录加入信任区再试。
</details>

<details>
<summary><b>有少数歌死活转不出来</b></summary>

部分歌曲需要先在 QQ音乐里播放一遍，让它完成解密加载，再运行 TuneFree。
</details>

<details>
<summary><b>支持 Mac / Linux 吗？</b></summary>

暂时只有 Windows。
</details>

## 从源码运行

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 把 ffmpeg.exe 放到本目录（程序会在这个目录找它）
#    Windows 版下载：https://www.gyan.dev/ffmpeg/builds/ （选 release essentials）

# 3. 启动图形界面
python TuneFree.py

# 或者用命令行
python main.py -i "D:\下载" -o "D:\音乐" -b 320k
```

命令行参数：

| 参数 | 说明 |
|---|---|
| `-i, --input` | 输入目录，默认当前目录 |
| `-o, --output` | 输出根目录，默认 `./output` |
| `--mp3-dir` / `--flac-dir` | 分别指定 MP3 / FLAC 输出位置 |
| `-b, --bitrate` | MP3 比特率，默认 `192k` |
| `--keep-flac` | 保留解密出的 FLAC |
| `--no-mp3` | 只输出 FLAC，不转 MP3 |
| `--force` | 已存在的 MP3 也重新生成 |
| `-v, --version` | 显示版本号 |

## 项目结构

```
TuneFree.py          图形界面（PySide6）
main.py              命令行 / 后端：注入、解密、转码
hook_qq_music.js     Frida 注入脚本（解密核心）
version.py           版本号与仓库地址的唯一来源
version.json         给国内用户用的更新源
docs/                官网
```

## 免责声明

本工具仅供**个人学习交流**使用，用于解密本机已合法下载的音乐文件，方便在自己拥有的设备上播放。

请勿用于传播、二次分发或商业用途。使用产生的一切法律责任由使用者自行承担。

## 鸣谢

- [Frida](https://frida.re/) — 进程注入
- [FFmpeg](https://ffmpeg.org/) — 音频转码
- [music-decryptor](https://github.com/ericjuice/music-decryptor) — 解密脚本的起点
- [Unlock Music](https://github.com/unlock-music/unlock-music) — 加密格式的解析经验
- [PyInstaller](https://pyinstaller.org/) — 打包

## 支持

- 🐛 [提 Issue](https://github.com/zksbx/TuneFree/issues)
- 💬 QQ 群：1067975918
- ☕ [爱发电](https://ifdian.net/a/TuneFree)
