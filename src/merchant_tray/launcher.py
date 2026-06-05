"""启动器：保持一个轻量入口，方便 bat 或打包脚本调用。"""


def main():
    from . import tray_app

    tray_app.main()


if __name__ == "__main__":
    main()
