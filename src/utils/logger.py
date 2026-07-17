from datetime import datetime

class Logger:
    RESET = "\033[0m"

    COLORS = {
        # background green
        "DEBUG": "\033[42m",

        # background blue
        "INFO": "\033[44m",

        # background yellow
        "WARNING": "\033[43m",

        # background red
        "ERROR": "\033[41m",
    }

    @classmethod
    def log(cls, level: str, message: str, module: str | None = None):
        color = cls.COLORS.get(level, "")
        now = datetime.now().strftime("%H:%M:%S")

        if module:
            print(f"{color}[{level:^7}]{cls.RESET}"
                  f"{now}"
                  f"[{module:^8.8}]"
                  f"{message}"
                  )
        else:
            print(
                f"{color}[{level:^7}]{cls.RESET}"
                f"{now}"
                f"{message}"
            )

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