"""日志: 所有时间戳统一打印美东时间 (America/New_York), 与策略时段一致。

部署在日本/任何时区的服务器上, 日志时间依然表示纽约时间,
避免跨时区排查问题时混淆。
"""

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")


class ETFormatter(logging.Formatter):
    def converter(self, *args):
        return datetime.now(ET_TZ).timetuple()

    def formatTime(self, record, datefmt=None):
        dt = datetime.now(ET_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{int(dt.microsecond/1000):03d}"


def setup_logging(log_dir: Path, name: str = "ztp", level: int = logging.INFO,
                  console: bool = True) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = ETFormatter(
        "%(asctime)s.%(msecs)03d [ET] %(levelname)-7s %(message)s", datefmt=""
    )
    fh = RotatingFileHandler(
        log_dir / "ztp.log", maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("ztp")
