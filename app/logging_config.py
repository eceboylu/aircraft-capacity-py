
import logging
import os

DEFAULT_LOG_LEVEL = "INFO"


def configure_logging(default_level: str = DEFAULT_LOG_LEVEL) -> None:
    level_name = os.environ.get("LOG_LEVEL", default_level).upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
