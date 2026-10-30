import frida
import os
import re
import sys
import argparse
import logging
import subprocess
import tempfile
import shutil
import time
import traceback
from pathlib import Path

try:  # 版本号统一放在 version.py，打包时如果没带上就退回内置值
    from version import VERSION
except Exception:
    VERSION = "2.1.2"

# -------------------- 强制 UTF-8 输出（解决编码错误） --------------------
def _force_utf8_stdio():
    """打包成 --noconsole 的 exe 时 sys.stdout 可能是 None，直接 fileno() 会崩。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            setattr(sys, name, open(stream.fileno(), mode='w', encoding='utf-8', errors='replace'))
        except (OSError, ValueError, AttributeError):
            pass


if sys.platform == 'win32':
    _force_utf8_stdio()

# -------------------- 资源路径辅助函数 --------------------
def resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    # 源码运行时按脚本所在目录找资源，别依赖调用方的工作目录
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

# -------------------- 文件删除重试（避免权限错误） --------------------
def force_remove(file_path, max_retries=5, delay=0.2):
    for _ in range(max_retries):
        try:
            if os.path.exists(file_path):
                os.unlink(file_path)
            return True
        except PermissionError:
            time.sleep(delay)
            continue
    return False


def _mkstemp_path(suffix):
    """建一个临时文件并立即关闭句柄。NamedTemporaryFile(...).name 的句柄会被占着。"""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


def emit_progress(phase, done, total):
    """
    进度协议：GUI 认 `@@PROGRESS 解密 3/10` 这一行并更新进度条（不写进日志）。
    在真正的控制台里（isatty）额外打一行可覆盖的进度，方便命令行用户。
    """
    print(f"@@PROGRESS {phase} {done}/{total}", flush=True)
    try:
        is_tty = sys.stdout is not None and sys.stdout.isatty()
    except (ValueError, AttributeError):
        is_tty = False
    if is_tty:
        print(f"{phase}: {done}/{total}", end='\r', flush=True)


# -------------------- 解密函数 --------------------
def run_decrypt(input_dir, flac_output_dir):
    if not os.path.exists(input_dir):
        logging.error(f"输入目录不存在: {input_dir}")
        return 0, 0

    input_dir = os.path.abspath(input_dir)
    flac_output_dir = os.path.abspath(flac_output_dir)

    try:
        session = frida.attach("QQMusic.exe")
    except frida.ProcessNotFoundError:
        logging.error("请确保QQ音乐客户端正在运行后重试")
        return 0, 0

    js_path = resource_path("hook_qq_music.js")
    with open(js_path, "r", encoding="utf-8") as f:
        jscode = f.read()
    script = session.create_script(jscode)
    script.load()
    logging.info("已连接到 QQMusic 进程")

    # 启动自检：QQ音乐一更新，EncAndDesMediaFile 的导出符号就可能失效。
    # 先探一次，直接告诉用户"版本不匹配"，而不是让他对着满屏 invalid size 猜。
    try:
        probe = script.exports_sync.probe()
        if not probe.get("ok", True):
            logging.error(str(probe.get("message", "QQ音乐版本与 TuneFree 不匹配")))
            logging.error("请把 QQ音乐降级到 TuneFree 适配的版本，或等 TuneFree 更新后再试")
            session.detach()
            return 0, 0
    except Exception as e:
        # 旧版 hook 没有 probe 导出，忽略即可，不影响正常解密
        logging.debug("跳过版本自检: %s", e)

    # 收集所有待处理文件（不区分大小写，Windows 上 .MFLAC 也认）
    file_list = []
    for root, _dirs, files in os.walk(input_dir):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in (".mflac", ".mgg"):
                file_list.append((root, file))

    total = len(file_list)
    if total == 0:
        logging.info(f"在 {input_dir} 里没有找到 .mflac 或 .mgg 文件")
        session.detach()
        return 0, 0

    os.makedirs(flac_output_dir, exist_ok=True)
    logging.info(f"找到 {total} 个文件，开始解密...")

    success = 0
    failed = 0
    skipped = 0

    try:
        for idx, (root, file) in enumerate(file_list, 1):
            stem, ext = os.path.splitext(file)
            new_ext = ".flac" if ext.lower() == ".mflac" else ".ogg"
            output_file_path = os.path.join(flac_output_dir, stem + new_ext)

            if os.path.exists(output_file_path):
                # 已解密过，跳过即可（后面照样会用它转码）
                skipped += 1
                emit_progress("解密", idx, total)
                continue

            src_path = os.path.join(root, file)
            temp_input_path = None
            temp_output_path = _mkstemp_path(new_ext)

            try:
                if src_path.isascii():
                    # 纯英文路径：直接解密，省掉一次整文件拷贝（大文件很省时间）
                    input_path = src_path
                else:
                    # 含中文/日文等：hook 里的 File 按 UTF-8 拼路径，先搬到 ASCII 临时文件更稳
                    temp_input_path = _mkstemp_path(ext.lower())
                    shutil.copy2(src_path, temp_input_path)
                    input_path = temp_input_path

                script.exports_sync.decrypt(input_path, temp_output_path)
                shutil.move(temp_output_path, output_file_path)
                success += 1
            except Exception as e:
                failed += 1
                logging.error(f"解密失败: {file} - {e}")
            finally:
                for leftover in (temp_input_path, temp_output_path):
                    if leftover and os.path.exists(leftover):
                        try:
                            os.unlink(leftover)
                        except OSError:
                            pass

            emit_progress("解密", idx, total)
    finally:
        # 任何异常都要断开，否则注入会一直挂在 QQ音乐 进程里
        try:
            session.detach()
        except Exception:
            pass

    logging.info(f"解密阶段完成: 成功 {success} 个，失败 {failed} 个")
    if skipped:
        logging.info(f"其中 {skipped} 个之前已解密过，直接复用（不需要重新解密）")
    return success, failed

# -------------------- 转码函数 --------------------
def explain_returncode(code):
    """把 ffmpeg/系统的返回码翻译成人话，方便用户自查。"""
    if code in (4294967283, -13):
        return ("权限不足/拒绝访问(EACCES)：常见原因是杀毒软件拦下了 ffmpeg.exe，"
                "或输出目录里的同名 .mp3 正被别的程序占用（播放器/网盘同步/资源管理器预览）")
    if code in (4294967295, -1):
        return "ffmpeg 内部错误，通常是输入文件损坏或未解密完整"
    if code in (4294967294, -2):
        return "找不到输入文件(ENOENT)，可能解密产物已被移走或删除"
    if code in (4294967264, -32):
        return "文件正被其他进程占用(EBUSY)"
    if code in (4294967263, -33):
        return "参数不合法(EINVAL)"
    if code == 1:
        return "ffmpeg 处理失败（输入不是有效音频，或输出路径不可写）"
    if code > 2147483647:
        return f"系统/驱动层错误(0x{code:08X})"
    return "未知错误"


def _run_ffmpeg(audio_path, out_path, ffmpeg_path, bitrate):
    """真正调用 ffmpeg，失败时抛 CalledProcessError。"""
    cmd = [ffmpeg_path, '-i', str(audio_path), '-b:a', bitrate, str(out_path), '-y']
    subprocess.run(cmd, check=True, capture_output=True, text=True,
                   encoding='utf-8', errors='ignore')


def _run_ffmpeg_to_bytes(audio_path, ffmpeg_path, bitrate):
    """
    让 ffmpeg 把 MP3 输出到标准输出，由 Python 负责落盘。
    有些安全软件会禁止 ffmpeg.exe 创建任何文件（连 %TEMP% 都不许），
    但读文件和写管道是放行的，这条路能绕过去。
    """
    cmd = [ffmpeg_path, '-i', str(audio_path), '-b:a', bitrate, '-f', 'mp3', 'pipe:1']
    proc = subprocess.run(cmd, check=True, capture_output=True)
    data = proc.stdout or b''
    if len(data) < 2048:
        raise RuntimeError(f"ffmpeg 只输出了 {len(data)} 字节，转码未成功")
    return data


def convert_to_mp3(audio_path, mp3_path, ffmpeg_path, bitrate='192k'):
    try:
        _run_ffmpeg(audio_path, mp3_path, ffmpeg_path, bitrate)
        return True
    except FileNotFoundError:
        logging.error("未找到 ffmpeg.exe（请确认 ffmpeg.exe 与 main.py 在同一目录）")
        return False
    except OSError as e:
        logging.error(f"无法运行 ffmpeg.exe: {e}"
                      "（多半是杀毒软件拦截，请把 TuneFree 目录加入白名单）")
        return False
    except subprocess.CalledProcessError as e:
        code = e.returncode
        stderr = e.stderr if isinstance(e.stderr, str) else (e.stderr or b'').decode('utf-8', 'ignore')
        detail = [x.strip() for x in stderr.splitlines() if x.strip()]

        # 写入被拒(EACCES/EPERM)：退一步，让 ffmpeg 把音频输出到管道，由 Python 自己写文件。
        if code in (4294967283, -13, 4294967291, -5):
            logging.warning(f"{Path(mp3_path).name}: ffmpeg 无法创建输出文件，"
                            f"改用「管道输出 + Python 落盘」重试…")
            try:
                data = _run_ffmpeg_to_bytes(audio_path, ffmpeg_path, bitrate)
                Path(mp3_path).write_bytes(data)
                logging.info(f"转码成功（管道模式）: {Path(mp3_path).name}")
                return True
            except Exception as e2:
                logging.error(f"  管道模式也失败: {e2}")

            # 再退一步：先写临时目录，再搬过去
            tmp = Path(tempfile.gettempdir()) / f"tf_{os.getpid()}_{Path(mp3_path).stem}.mp3"
            try:
                _run_ffmpeg(audio_path, str(tmp), ffmpeg_path, bitrate)
                shutil.move(str(tmp), str(mp3_path))
                logging.info(f"转码成功（经临时目录中转）: {Path(mp3_path).name}")
                return True
            except Exception as e3:
                logging.error(f"转码失败 {Path(audio_path).name}，返回码: {code}"
                              f"（{explain_returncode(code)}）")
                for line in detail[-4:]:
                    logging.error(f"  ffmpeg: {line}")
                logging.error(f"  经临时目录重试仍失败: {e3}")
                return False
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass

        logging.error(f"转码失败 {Path(audio_path).name}，返回码: {code}"
                      f"（{explain_returncode(code)}）")
        for line in detail[-4:]:
            logging.error(f"  ffmpeg: {line}")
        return False

def process_audio_to_mp3(audio_dir, mp3_dir, ffmpeg_path, bitrate='192k',
                         keep_flac=False, no_mp3=False, force=False):
    """
    处理 audio_dir 中的所有 flac 和 ogg 文件，根据模式转码为 MP3。
    如果 no_mp3=True：
        - FLAC 文件不转码（保留原文件）
        - OGG 文件强制转码为 MP3，且删除 OGG 源文件（不保留）
    否则：
        - 所有文件都转码，并根据 keep_flac 决定是否删除源文件
    force=True：已经存在的 MP3 也重新生成（默认跳过，避免重复劳动）。
    """
    flac_files = sorted(Path(audio_dir).glob('*.flac'))
    ogg_files = sorted(Path(audio_dir).glob('*.ogg'))
    all_files = flac_files + ogg_files
    if not all_files:
        logging.info(f"在 {audio_dir} 中没有找到 .flac 或 .ogg 文件")
        return 0, 0

    os.makedirs(mp3_dir, exist_ok=True)
    probe = Path(mp3_dir) / ".tunefree_write_probe"
    try:
        probe.write_bytes(b"0")
        probe.unlink()
    except Exception as e:
        logging.warning(f"输出目录 {mp3_dir} 看起来无法直接写入（{e}），"
                        f"稍后会自动改为「管道输出 + Python 落盘」，速度会慢一点")

    total = len(all_files)
    success = 0
    failed = 0
    skipped = 0

    logging.info(f"找到 {total} 个音频文件，开始转码 MP3...")

    for idx, audio_file in enumerate(all_files, 1):
        ext = audio_file.suffix.lower()
        mp3_file = Path(mp3_dir) / (audio_file.stem + '.mp3')

        if no_mp3 and ext == '.flac':
            # 仅 FLAC 模式：FLAC 原样保留，不用转码
            success += 1
            emit_progress("转码", idx, total)
            continue

        # 仅 FLAC 模式下的 OGG 必须转 MP3 且不保留源文件；其余情况看 keep_flac
        should_keep_original = keep_flac if not no_mp3 else False

        if mp3_file.exists() and not force:
            skipped += 1
            if not should_keep_original:
                force_remove(str(audio_file))
            emit_progress("转码", idx, total)
            continue

        if convert_to_mp3(str(audio_file), str(mp3_file), ffmpeg_path, bitrate):
            success += 1
            if not should_keep_original and not force_remove(str(audio_file)):
                logging.warning(f"源文件删除失败（可能被播放器占用），已保留: {audio_file.name}")
        else:
            failed += 1
        emit_progress("转码", idx, total)

    logging.info(f"转码阶段完成: 成功 {success} 个，失败 {failed} 个")
    if skipped:
        logging.info(f"其中 {skipped} 个 MP3 之前已生成，直接跳过（要重新生成请加 --force）")
    return success, failed

# -------------------- 主函数 --------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='解密 QQ音乐 mflac/mgg 文件并转换为 MP3')
    parser.add_argument("-i", "--input", help="输入目录（包含 .mflac / .mgg 文件），默认为当前目录")
    parser.add_argument("-o", "--output", help="输出根目录（默认：当前目录下的 output），其下自动创建 mp3 与 flac 两个子文件夹")
    parser.add_argument("--flac-dir", help="FLAC 输出目录（仅在 --keep-flac 时有效）")
    parser.add_argument("--mp3-dir", help="MP3 输出目录（覆盖默认）")
    parser.add_argument("-b", "--bitrate", default="192k", help="MP3 比特率，例如 128k, 192k, 320k (默认: 192k)")
    parser.add_argument("--keep-flac", action="store_true", help="保留解密后的 FLAC 文件（默认不保留）")
    parser.add_argument("--no-mp3", action="store_true", help="不生成 MP3，仅保留 FLAC（忽略 --keep-flac）")
    parser.add_argument("--force", action="store_true", help="已经存在的 MP3 也重新生成（默认跳过）")
    parser.add_argument("-v", "--version", action="version", version=f"TuneFree {VERSION}")
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')
    args = parse_args(argv)

    logging.info(f"TuneFree {VERSION}")

    # 比特率只接受 128k / 320k 这种写法，别把奇怪的值传给 ffmpeg
    bitrate = (args.bitrate or "192k").strip().lower()
    if not re.fullmatch(r"\d{2,3}k", bitrate):
        logging.warning(f"比特率 {args.bitrate!r} 不合法，已改用 192k")
        bitrate = "192k"
    else:
        kbps = int(bitrate[:-1])
        if kbps > 320:
            logging.warning(f"比特率 {bitrate} 超出 MP3 上限，已改为 320k")
            bitrate = "320k"
        elif kbps < 32:
            logging.warning(f"比特率 {bitrate} 过低，已改为 192k")
            bitrate = "192k"

    # 确定输入目录
    input_dir = os.path.abspath(args.input) if args.input else os.getcwd()
    if not os.path.isdir(input_dir):
        logging.error(f"输入目录不存在: {input_dir}")
        return 1

    # 确定输出目录（两个子目录各自独立，只给一个也能用）
    if args.output:
        root_output = os.path.abspath(args.output)
    else:
        root_output = os.path.join(os.getcwd(), "output")

    flac_output_dir = (os.path.abspath(args.flac_dir) if args.flac_dir
                       else os.path.join(root_output, "flac"))
    mp3_output_dir = (os.path.abspath(args.mp3_dir) if args.mp3_dir
                      else os.path.join(root_output, "mp3"))

    logging.info(f"输入目录: {input_dir}")
    logging.info(f"FLAC 输出: {flac_output_dir}")
    logging.info(f"MP3 输出: {mp3_output_dir}")

    # 获取 ffmpeg 路径
    ffmpeg_path = resource_path("ffmpeg.exe")
    if not os.path.exists(ffmpeg_path):
        logging.error(f"未找到 ffmpeg.exe，请将其放在 {ffmpeg_path} 路径")
        return 1

    # 解密
    dec_success, dec_failed = run_decrypt(input_dir, flac_output_dir)

    # 转码：只要 FLAC 目录里确实有音频文件就继续（之前解过、这次被跳过的也算）
    has_audio = bool(list(Path(flac_output_dir).glob('*.flac')) or
                     list(Path(flac_output_dir).glob('*.ogg')))
    if has_audio:
        process_audio_to_mp3(flac_output_dir, mp3_output_dir, ffmpeg_path,
                             bitrate, args.keep_flac, args.no_mp3, args.force)
    elif dec_failed:
        logging.error("解密全部失败，没有可转码的文件；请查看上面的失败原因")
    else:
        logging.info("没有可用的 FLAC/OGG 文件，跳过转码步骤")
        logging.info("小提示：确认选择的目录里确实有 .mflac / .mgg 文件（可以包含子文件夹）")

    if dec_success and dec_failed:
        logging.info(f"解密 {dec_success} 个成功、{dec_failed} 个失败（失败的文件名见上文）")
    logging.info("全部处理完成")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logging.warning("已被用户中断")
        sys.exit(130)
    except Exception:  # 兜底：别让 GUI 只看到一个莫名其妙的返回码
        logging.error("程序异常终止，下面是详细信息：")
        traceback.print_exc()
        sys.exit(1)


# 发行者：曾困睡不醒
# 发行日期：26.3.8