"""Discovery diagnostics use the existing process log; activity callbacks show errors."""
import logging
import os


def configure_discovery_logging() -> None:
    """Enable detailed discovery with CHIDVI_LOG_LEVEL=DEBUG, without SDK debug logs."""
    level = logging.DEBUG if os.environ.get("CHIDVI_LOG_LEVEL", "").strip().upper() == "DEBUG" else logging.INFO
    for name in ("core.action_loader", "core.plugin_loader"):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False
        if not any(getattr(handler, "_chidvi_discovery", False) for handler in logger.handlers):
            handler = logging.StreamHandler()
            handler._chidvi_discovery = True
            handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
            logger.addHandler(handler)
