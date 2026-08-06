"""Django settings for tuxwsmaker."""

import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
env = environ.Env(
    DEBUG=(bool, False),
    SECRET_KEY=(str, "change-me"),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    DB_ENGINE=(str, "sqlite"),
    DB_NAME=(str, str(BASE_DIR / "db.sqlite3")),
    DB_USER=(str, ""),
    DB_PASSWORD=(str, ""),
    DB_HOST=(str, "localhost"),
    DB_PORT=(str, "3306"),
    REDIS_URL=(str, "redis://localhost:6379/0"),
    BUILD_VM_SSH_USER=(str, "root"),
    BUILD_NETWORK_NAME=(str, "wsbuildnet"),
    BUILDER_VM_NAME=(str, "tuxwsmaker-builder"),
    BUILDER_HYPERVISOR_URI=(str, "qemu:///system"),
    BUILDER_LIBVIRT_NETWORK=(str, "wsbuildnet"),
    BUILDER_LIBVIRT_ENABLE_DHCP=(bool, False),
    BUILDER_LIBVIRT_ENABLE_DNS=(bool, False),
    BUILDER_LIBVIRT_BRIDGE_NAME=(str, "virbrwsbld"),
    BUILDER_LIBVIRT_NETWORK_GATEWAY=(str, "192.168.200.1"),
    BUILDER_LIBVIRT_NETWORK_NETMASK=(str, "255.255.255.0"),
    BUILDER_LIBVIRT_DHCP_START=(str, "192.168.200.100"),
    BUILDER_LIBVIRT_DHCP_END=(str, "192.168.200.254"),
    BUILDER_VM_STATIC_IPV4=(str, "192.168.200.10"),
    BUILDER_VM_STATIC_PREFIX=(int, 24),
    BUILDER_VM_NIC_MAC=(str, "52:54:00:c8:00:0a"),
    BUILDER_VM_BOOTSTRAP_IP=(str, "192.168.200.10"),
    BUILDER_STORAGE_POOL_NAME=(str, "default"),
    BUILDER_STORAGE_POOL_PATH=(str, "/var/lib/libvirt/images"),
    BUILDER_VM_MEMORY_MIB=(int, 2048),
    BUILDER_VM_VCPUS=(int, 1),
    BUILDER_VM_DISK_GIB=(int, 80),
    BUILDER_BASE_IMAGE_PATH=(str, ""),
    BUILDER_SHARED_ISO_ROOT=(str, str(BASE_DIR / "media" / "isos")),
    BUILDER_VM_DISK_PATH=(str, str(BASE_DIR / "media" / "builder" / "builder.qcow2")),
    TIME_ZONE=(str, "UTC"),
    CONCURRENT_BUILDS_DEFAULT=(int, 2),
)

environ.Env.read_env(BASE_DIR / ".env")


SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "rest_framework",
    "apps.users",
    "apps.catalog",
    "apps.layouts",
    "apps.packages",
    "apps.playbooks",
    "apps.repositories",
    "apps.afterburners",
    "apps.builds",
    "apps.serverconfig",
    "apps.api",
    "apps.workers",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "config.middleware.BackfillCSRFMiddlewareTokenMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

AUTH_USER_MODEL = "users.User"

AUTHENTICATION_BACKENDS = [
    "apps.users.backends.LDAPBackend",
    "apps.users.backends.LocalModelBackend",
]

LOGIN_URL = "/users/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/users/login/"


if env("DB_ENGINE").lower() == "mariadb":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": env("DB_NAME"),
            "USER": env("DB_USER"),
            "PASSWORD": env("DB_PASSWORD"),
            "HOST": env("DB_HOST"),
            "PORT": env("DB_PORT"),
            "OPTIONS": {
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# Password validation
# https://docs.djangoproject.com/en/5.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE")

USE_I18N = True

USE_TZ = True


STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

ISO_UPLOAD_ROOT = MEDIA_ROOT / "isos"
ARTIFACT_ROOT = MEDIA_ROOT / "artifacts"

CONCURRENT_BUILDS_DEFAULT = env("CONCURRENT_BUILDS_DEFAULT")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

REDIS_URL = env("REDIS_URL")
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
        },
    }
}
BUILD_VM_SSH_USER = env("BUILD_VM_SSH_USER")
BUILD_NETWORK_NAME = env("BUILD_NETWORK_NAME")
BUILDER_VM_NAME = env("BUILDER_VM_NAME")
BUILDER_HYPERVISOR_URI = env("BUILDER_HYPERVISOR_URI")
BUILDER_LIBVIRT_NETWORK = env("BUILDER_LIBVIRT_NETWORK")
BUILDER_LIBVIRT_ENABLE_DHCP = env("BUILDER_LIBVIRT_ENABLE_DHCP")
BUILDER_LIBVIRT_ENABLE_DNS = env("BUILDER_LIBVIRT_ENABLE_DNS")
BUILDER_LIBVIRT_BRIDGE_NAME = env("BUILDER_LIBVIRT_BRIDGE_NAME")
BUILDER_LIBVIRT_NETWORK_GATEWAY = env("BUILDER_LIBVIRT_NETWORK_GATEWAY")
BUILDER_LIBVIRT_NETWORK_NETMASK = env("BUILDER_LIBVIRT_NETWORK_NETMASK")
BUILDER_LIBVIRT_DHCP_START = env("BUILDER_LIBVIRT_DHCP_START")
BUILDER_LIBVIRT_DHCP_END = env("BUILDER_LIBVIRT_DHCP_END")
BUILDER_VM_STATIC_IPV4 = env("BUILDER_VM_STATIC_IPV4")
BUILDER_VM_STATIC_PREFIX = env("BUILDER_VM_STATIC_PREFIX")
BUILDER_VM_NIC_MAC = env("BUILDER_VM_NIC_MAC")
BUILDER_VM_BOOTSTRAP_IP = env("BUILDER_VM_BOOTSTRAP_IP")
BUILDER_STORAGE_POOL_NAME = env("BUILDER_STORAGE_POOL_NAME")
BUILDER_STORAGE_POOL_PATH = Path(env("BUILDER_STORAGE_POOL_PATH"))
BUILDER_VM_MEMORY_MIB = env("BUILDER_VM_MEMORY_MIB")
BUILDER_VM_VCPUS = env("BUILDER_VM_VCPUS")
BUILDER_VM_DISK_GIB = env("BUILDER_VM_DISK_GIB")
BUILDER_BASE_IMAGE_PATH = env("BUILDER_BASE_IMAGE_PATH")
BUILDER_SHARED_ISO_ROOT = Path(env("BUILDER_SHARED_ISO_ROOT"))
BUILDER_VM_DISK_PATH = Path(env("BUILDER_VM_DISK_PATH"))
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 60
RHSM_DISCOVERY_RHEL_VERSIONS = [
    int(v)
    for v in env.list("RHSM_DISCOVERY_RHEL_VERSIONS", default=["8", "9", "10"])
    if str(v).strip()
]
RHSM_DISCOVERY_ARCH = env("RHSM_DISCOVERY_ARCH", default="x86_64")
RHSM_DISCOVERY_USERNAME = env("RHSM_DISCOVERY_USERNAME", default="")
RHSM_DISCOVERY_PASSWORD = env("RHSM_DISCOVERY_PASSWORD", default="")
RHSM_DISCOVERY_ORG_ID = env("RHSM_DISCOVERY_ORG_ID", default="")
RHSM_DISCOVERY_ACTIVATION_KEY = env("RHSM_DISCOVERY_ACTIVATION_KEY", default="")
RHSM_REPO_SYNC_INTERVAL_SECONDS = env.int("RHSM_REPO_SYNC_INTERVAL_SECONDS", default=60 * 60)
CELERY_BEAT_SCHEDULE = {
    "sync-rhsm-repository-catalog": {
        "task": "repositories.sync_rhsm_repository_catalog",
        "schedule": RHSM_REPO_SYNC_INTERVAL_SECONDS,
    },
}

CSRF_TRUSTED_ORIGINS = [v for v in env.list("CSRF_TRUSTED_ORIGINS", default=[]) if v]

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
