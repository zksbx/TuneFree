"""TuneFree 唯一入口。

- 不带参数（双击）        -> 图形界面
- 带 --cli 及其它命令行参数 -> 命令行后端（界面会以这种方式再起一个自己）

这样打包出来只有一个 exe，不会再有 main.exe。
"""
import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] == "--cli":
        # 命令行模式：不导入 PySide6，启动更快
        from main import main as cli_main
        return cli_main(args[1:])

    # 图形界面模式
    from TuneFree import run_gui
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
