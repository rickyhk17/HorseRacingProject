"""Read optional local Telegram credentials without executing configuration."""

import os
import stat
from pathlib import Path


CREDENTIALS_FILE = Path(__file__).parent / "telegram.credentials"
KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
MAX_BYTES = 4096


class CredentialsFileError(ValueError):
    pass


def read_credentials_file():
    """Read a private, regular file; a missing file is equivalent to no values."""
    try:
        fd = os.open(CREDENTIALS_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise CredentialsFileError("Cannot safely read Telegram credentials file.") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise CredentialsFileError("Telegram credentials file must be private and owner-controlled.")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise CredentialsFileError("Invalid Telegram credentials file.")
        values = {}
        for line in data.decode("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key not in KEYS or key in values:
                raise CredentialsFileError("Invalid Telegram credentials file.")
            values[key] = value
        return values
    except (OSError, UnicodeError):
        raise CredentialsFileError("Cannot safely read Telegram credentials file.") from None
    finally:
        if fd is not None:
            os.close(fd)


def credential_values():
    # Explicit environment values, including empty values, always take precedence.
    values = {key: os.environ[key] for key in KEYS if key in os.environ}
    if len(values) != len(KEYS):
        file_values = read_credentials_file()
        for key in KEYS:
            if key not in values:
                values[key] = file_values.get(key, "")
    return values["TELEGRAM_BOT_TOKEN"], values["TELEGRAM_CHAT_ID"]
