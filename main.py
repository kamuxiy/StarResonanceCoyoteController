import sys
import os
import traceback
from datetime import datetime
from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtGui import QFont
from PyQt5.QtCore import Qt

from config_window import ConfigWindow


def debug_log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[DEBUG {timestamp}] {message}", flush=True)


def global_exception_handler(exc_type, exc_value, exc_traceback):
    debug_log("=" * 60)
    debug_log("未捕获的异常！")
    debug_log(f"异常类型: {exc_type.__name__}")
    debug_log(f"异常信息: {exc_value}")
    debug_log("堆栈跟踪:")
    for line in traceback.format_exception(exc_type, exc_value, exc_traceback):
        debug_log(f"  {line.rstrip()}")
    debug_log("=" * 60)
    
    try:
        error_msg = f"程序发生未捕获的异常:\n{exc_type.__name__}: {exc_value}\n\n详情请查看控制台输出。"
        QMessageBox.critical(None, "程序错误", error_msg)
    except Exception:
        pass
    
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


def main():
    sys.excepthook = global_exception_handler
    
    debug_log("=" * 60)
    debug_log("程序启动")
    debug_log(f"Python 版本: {sys.version}")
    debug_log(f"工作目录: {os.getcwd()}")
    debug_log(f"脚本路径: {os.path.abspath(__file__)}")
    debug_log("=" * 60)

    try:
        app = QApplication(sys.argv)
        app.setFont(QFont("Microsoft YaHei", 9))
        app.setStyle("Fusion")
        debug_log("QApplication 创建成功")
    except Exception as e:
        debug_log(f"QApplication 创建失败: {e}")
        debug_log(traceback.format_exc())
        raise

    try:
        window = ConfigWindow()
        debug_log("ConfigWindow 创建成功")
        window.show()
        debug_log("主窗口已显示")
    except Exception as e:
        debug_log(f"主窗口创建失败: {e}")
        debug_log(traceback.format_exc())
        raise

    debug_log("进入事件循环")
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
