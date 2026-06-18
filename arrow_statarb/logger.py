"""
Logging configuration using loguru
"""

import sys
from pathlib import Path

from loguru import logger


def setup_logging(
    log_dir: str = "logs",
    level: str = "INFO",
    rotation: str = "500 MB",
    retention: str = "30 days",
):
    """
    Configure application logging.

    Args:
        log_dir: Directory for log files
        level: Minimum log level
        rotation: When to rotate log files
        retention: How long to keep old logs
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Remove default handler
    logger.remove()

    # Console handler
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        colorize=True,
    )

    # Application log
    logger.add(
        str(log_path / "app.log"),
        level=level,
        rotation=rotation,
        retention=retention,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    )

    # Trade log (INFO and above from execution modules)
    logger.add(
        str(log_path / "trades.log"),
        level="INFO",
        rotation=rotation,
        retention=retention,
        filter=lambda record: "execution" in record["name"] or "trade" in record["message"].lower(),
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
    )

    # Error log
    logger.add(
        str(log_path / "errors.log"),
        level="ERROR",
        rotation=rotation,
        retention=retention,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}\n{exception}",
        backtrace=True,
        diagnose=True,
    )

    logger.info(f"Logging initialized (level={level}, dir={log_dir})")
