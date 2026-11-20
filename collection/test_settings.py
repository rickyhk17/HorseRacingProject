from config.settings import *  # noqa: F403

INSTALLED_APPS = [*INSTALLED_APPS]  # noqa: F405
if "collection" not in INSTALLED_APPS:
    INSTALLED_APPS.append("collection")
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
ALLOWED_HOSTS = ["testserver"]
SECRET_KEY = "collection-tests-only"
