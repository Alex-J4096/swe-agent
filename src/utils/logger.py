from src.ui.console import ConsoleRenderer

class Logger:
    @classmethod
    def log(cls, level: str, message: str, module: str | None = None):
        ConsoleRenderer().show_log(level, message, module)

    @classmethod
    def debug(cls, module: str, message: str):
        cls.log("DEBUG", message, module)

    @classmethod
    def info(cls, message: str, module: str | None = None):
        cls.log("INFO", message, module)

    @classmethod
    def warning(cls, message: str, module: str | None = None):
        cls.log("WARNING", message, module)

    @classmethod
    def error(cls, message: str, module: str | None = None):
        cls.log("ERROR", message, module)
