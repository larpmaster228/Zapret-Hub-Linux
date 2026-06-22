from __future__ import annotations

import errno
import webbrowser

from typing import Optional, Tuple, Callable


MSG_PORT_BUSY = (
    "Не удалось запустить прокси:\n"
    "Порт уже используется другим приложением.\n\n"
    "Закройте приложение, использующее этот порт, "
    "или измените порт в настройках прокси и перезапустите."
)

MSG_PERMISSION = (
    "Не удалось запустить прокси:\n"
    "Доступ к адресу/порту запрещён "
    "(брандмауэр, антивирус или права доступа).\n\n"
    "Измените порт на случайный в диапазоне 10000–50000 в настройках, "
    "проверьте брандмауэр/антивирус и перезапустите."
)

MSG_BAD_ADDRESS = (
    "Не удалось запустить прокси:\n"
    "Некорректный или недоступный адрес для прослушивания.\n\n"
    "Проверьте решение по открывшейся в браузере ссылке.\n"
    "Проверьте host и порт в настройках прокси и перезапустите."
)

# Windows WinSock error codes (exc.winerror); errno may differ from POSIX.
_WSA_EACCES = 10013
_WSA_EFAULT = 10014
_WSA_EADDRINUSE = 10048
_WSA_EADDRNOTAVAIL = 10049


def diagnose_listen_error(exc: BaseException) -> Tuple[Optional[str], Optional[Callable]]:
    """Map a listen-socket bind failure to a user-facing message.

    Returns None when the exception is not a recognizable bind failure,
    so callers can fall back to generic handling.
    """
    if not isinstance(exc, OSError):
        return None

    err = exc.errno
    winerror = getattr(exc, "winerror", None)

    if err == errno.EADDRINUSE or winerror == _WSA_EADDRINUSE:
        return MSG_PORT_BUSY, None
    if err == errno.EACCES or winerror == _WSA_EACCES:
        return MSG_PERMISSION, None
    if (winerror in (_WSA_EFAULT, _WSA_EADDRNOTAVAIL)
            or err in (errno.EADDRNOTAVAIL, errno.EFAULT)):
        return MSG_BAD_ADDRESS, lambda : webbrowser.open("https://github.com/Flowseal/tg-ws-proxy/issues/903#issuecomment-4726752103")
    return None, None
