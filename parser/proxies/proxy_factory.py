from loguru import logger

from dto import AvitoConfig
from .proxy import NoProxy, ServerProxy, MobileProxy, Proxy
from .modem_proxy import UsbModemProxy


def build_proxy(config: AvitoConfig, stop_event=None, notify_event=None) -> Proxy:
    """
    Определяет тип прокси
    """
    if getattr(config, "use_usb_modem", False):
        if config.proxy_string:
            logger.warning(
                "Одновременно включены USB-модем и прокси. "
                "Использую USB-модем, настройки прокси игнорируются."
            )
        logger.info("Трафик направляется через USB 4G модем")
        return UsbModemProxy(
            config, stop_event=stop_event, notify_event=notify_event
        )

    if config.proxy_change_url and not config.proxy_string:
        raise ValueError("proxy_change_url указан без proxy_string")

    if config.proxy_string and config.proxy_change_url:
        logger.info("Прокси определен как мобильный")
        return MobileProxy(config.proxy_string, config.proxy_change_url)

    if config.proxy_string:
        logger.info("Прокси определен как серверный")
        return ServerProxy(config.proxy_string)

    return NoProxy()
