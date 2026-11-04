# -*- coding: utf-8 -*-
"""
Production Configurations

"""

import ldap
from django_auth_ldap.config import LDAPSearch, GroupOfNamesType
from .common import *  # noqa
from environs import Env, EnvError
from core.storage import verify_storage_connection
from core.cache import verify_cache_connection

# Normally you should not import ANYTHING from Django directly
# into your settings, but ImproperlyConfigured is an exception.
from django.core.exceptions import ImproperlyConfigured


# Environment variables-based secrets
env = Env()
env.read_env()  # also read .env file, if it exists


def get_secret(setting, secret_type=env, default=None):
    """Get and return the secret variable or rise explicit exception."""
    try:
        return secret_type(setting) if default is None else secret_type(setting,
                                                                        default=default)
    except EnvError as e:
        # EnvError is the common base of EnvValidationError (the value is present but
        # unparseable) and EnvNotSetError (the variable is missing).
        raise ImproperlyConfigured(str(e))


# SECRET CONFIGURATION
# ------------------------------------------------------------------------------
# See: https://docs.djangoproject.com/en/6.0/ref/settings/#secret-key
# Raises ImproperlyConfigured exception if DJANGO_SECRET_KEY not in os.environ
SECRET_KEY = get_secret('DJANGO_SECRET_KEY')


# SUPERUSER SETTINGS
CHRIS_SUPERUSER_PASSWORD = get_secret('CHRIS_SUPERUSER_PASSWORD')


# SITE CONFIGURATION
# ------------------------------------------------------------------------------
# Hosts/domain names that are valid for this site
# See https://docs.djangoproject.com/en/6.0/ref/settings/#allowed-hosts
ALLOWED_HOSTS = get_secret('DJANGO_ALLOWED_HOSTS', env.list)
# END SITE CONFIGURATION


# DATABASE CONFIGURATION
# ------------------------------------------------------------------------------
DATABASES['default']['NAME'] = get_secret('POSTGRES_DB')
DATABASES['default']['USER'] = get_secret('POSTGRES_USER')
DATABASES['default']['PASSWORD'] = get_secret('POSTGRES_PASSWORD')
DATABASES['default']['HOST'] = get_secret('DATABASE_HOST')
DATABASES['default']['PORT'] = get_secret('DATABASE_PORT')
DATABASE_CONN_POOL = get_secret('DATABASE_CONN_POOL', env.bool)

if DATABASE_CONN_POOL:
    DATABASE_CONN_POOL_MIN_SIZE = get_secret('DATABASE_CONN_POOL_MIN_SIZE', env.int)
    DATABASE_CONN_POOL_MAX_SIZE = get_secret('DATABASE_CONN_POOL_MAX_SIZE', env.int)
    DATABASE_CONN_POOL_TIMEOUT = get_secret('DATABASE_CONN_POOL_TIMEOUT', env.float)
    DATABASES['default']['OPTIONS'] = {'pool': {'min_size': DATABASE_CONN_POOL_MIN_SIZE,
                                                'max_size': DATABASE_CONN_POOL_MAX_SIZE,
                                                'timeout': DATABASE_CONN_POOL_TIMEOUT}}

# Apply the QIDO-RS fuzzy-matching similarity threshold GUC (see common.py),
# whether or not the connection pool branch above ran.
apply_dicomweb_fuzzy_threshold(DATABASES['default'])  # noqa: F405


# STORAGE CONFIGURATION
# ------------------------------------------------------------------------------
STORAGE_ENV = get_secret('STORAGE_ENV')

if STORAGE_ENV not in ('swift', 'fslink', 'filesystem', 's3'):
    raise ImproperlyConfigured(f"Unsupported value '{STORAGE_ENV}' for STORAGE_ENV")

if STORAGE_ENV == 'swift':
    STORAGES['default'] = {'BACKEND': 'swift.storage.SwiftStorage'}
    SWIFT_AUTH_URL = get_secret('SWIFT_AUTH_URL')
    SWIFT_USERNAME = get_secret('SWIFT_USERNAME')
    SWIFT_KEY = get_secret('SWIFT_KEY')
    SWIFT_CONTAINER_NAME = get_secret('SWIFT_CONTAINER_NAME')
    SWIFT_CONNECTION_PARAMS = {'user': SWIFT_USERNAME,
                               'key': SWIFT_KEY,
                               'authurl': SWIFT_AUTH_URL}
    verify_storage = lambda: verify_storage_connection(
        STORAGES=STORAGES,
        SWIFT_CONTAINER_NAME=SWIFT_CONTAINER_NAME,
        SWIFT_CONNECTION_PARAMS=SWIFT_CONNECTION_PARAMS
    )
elif STORAGE_ENV == 's3':
    STORAGES['default'] = {'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage'}
    S3_BUCKET_NAME = get_secret('S3_BUCKET_NAME')
    S3_CONNECTION_PARAMS = {
        'endpoint_url': get_secret('S3_ENDPOINT_URL'),
        'access_key': get_secret('S3_ACCESS_KEY'),
        'secret_key': get_secret('S3_SECRET_KEY'),
        'region_name': get_secret('S3_REGION', default=''),
    }
    # django-storages S3 settings (derived from ChRIS S3 config above)
    AWS_STORAGE_BUCKET_NAME = S3_BUCKET_NAME
    AWS_S3_ENDPOINT_URL = S3_CONNECTION_PARAMS['endpoint_url']
    AWS_ACCESS_KEY_ID = S3_CONNECTION_PARAMS['access_key']
    AWS_SECRET_ACCESS_KEY = S3_CONNECTION_PARAMS['secret_key']
    AWS_S3_REGION_NAME = S3_CONNECTION_PARAMS.get('region_name') or 'us-east-1'
    AWS_S3_ADDRESSING_STYLE = 'path'
    AWS_S3_SIGNATURE_VERSION = 's3v4'
    verify_storage = lambda: verify_storage_connection(
        STORAGES=STORAGES,
        S3_BUCKET_NAME=S3_BUCKET_NAME,
        S3_CONNECTION_PARAMS=S3_CONNECTION_PARAMS,
    )
elif STORAGE_ENV in ('fslink', 'filesystem'):
    STORAGES['default'] = {'BACKEND': 'django.core.files.storage.FileSystemStorage'}
    MEDIA_ROOT = get_secret('MEDIA_ROOT')
    verify_storage = lambda: verify_storage_connection(STORAGES=STORAGES,
                                                       MEDIA_ROOT=MEDIA_ROOT)
else:
    verify_storage = lambda: verify_storage_connection()

try:
    verify_storage()
except Exception as e:
    raise ImproperlyConfigured(str(e))


# CHRIS STORE SERVICE CONFIGURATION
CHRIS_STORE_URL = get_secret('CHRIS_STORE_URL')


# LOGGING CONFIGURATION
# See https://docs.djangoproject.com/en/6.0/topics/logging/ for
# more details on how to customize your logging configuration.
ADMINS = ['"FNNDSC Developers" <dev@babymri.org>']
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'console': {
            'format': '[%(levelname)s][%(module)s %(process)d %(thread)d] %(message)s'
        },
    },
    'handlers': {
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'console',
        },
    },
    'loggers': {
        '': {  # root logger
            'level': 'INFO',
            'handlers': ['console'],
        }
    }
}


# CORSHEADERS
# ------------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = get_secret('DJANGO_CORS_ALLOW_ALL_ORIGINS', env.bool,
                                    default=False)
CORS_ALLOWED_ORIGINS = get_secret('DJANGO_CORS_ALLOWED_ORIGINS', env.list,
                                  default=[])
CORS_ALLOWED_ORIGIN_REGEXES = get_secret('DJANGO_CORS_ALLOWED_ORIGIN_REGEXES',
                                         env.list, default=CORS_ALLOWED_ORIGINS)

# client checks Allow header to know if sign-ups are allowed by getting api/v1/users/
# https://github.com/FNNDSC/ChRIS_ui/commit/dd045b3e678bd5338567aeea4eaa4551a27bb63f
CORS_EXPOSE_HEADERS = ('Allow',) 


# NATS SETTINGS
# ------------------------------------------------------------------------------
NATS_ADDRESS = get_secret('NATS_ADDRESS')


# PFDCM SETTINGS
# ------------------------------------------------------------------------------
PFDCM_ADDRESS = get_secret('PFDCM_ADDRESS')


# CELERY SETTINGS
# ------------------------------------------------------------------------------
CELERY_BROKER_URL = get_secret('CELERY_BROKER_URL')

#: Only add pickle to this list if your broker is secured
#: from unwanted access (see userguide/security.html)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'

# Worker settings
# messages to prefetch at a time multiplied by the number of concurrent processes
# default is 4 (four messages for each process)
CELERYD_PREFETCH_MULTIPLIER = 2


# CACHE SETTINGS (see common.py)
# ------------------------------------------------------------------------------
# Optional until a feature needs the cache: without a URL nothing is cached, no process
# connects to a cache server and the helpers in core/cache.py go ahead without their locks,
# markers and slots. Otherwise a Redis-protocol server of the cache's own, apart from the
# Celery broker's: the start-up check refuses the broker's host and port as written,
# whatever the database, so an IP address or an alias gets past it. For example
# redis://chris-cache:6379/0. TLS options and socket timeouts (above 0, at most 5 s)
# can go in its query string, retry options cannot.
_cache_url = get_secret('CACHE_URL', default='')

if _cache_url:
    CACHES['default']['LOCATION'] = _cache_url  # noqa: F405
    verify_cache_connection(CACHES['default'], CELERY_BROKER_URL)  # noqa: F405
else:
    CACHES['default']['BACKEND'] = (  # noqa: F405
        'django.core.cache.backends.dummy.DummyCache')


# REVERSE PROXY
# ------------------------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = get_secret('DJANGO_SECURE_PROXY_SSL_HEADER', env.list)
SECURE_PROXY_SSL_HEADER = tuple(SECURE_PROXY_SSL_HEADER) if SECURE_PROXY_SSL_HEADER else None
USE_X_FORWARDED_HOST = get_secret('DJANGO_USE_X_FORWARDED_HOST', env.bool)


# LDAP AUTH CONFIGURATION
# ------------------------------------------------------------------------------
AUTH_LDAP = get_secret('AUTH_LDAP', env.bool)

if AUTH_LDAP:
    if not get_secret('AUTH_LDAP_VERIFY_TLS', env.bool, default=True):
        ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)
        ldap.set_option(ldap.OPT_X_TLS_NEWCTX, 0)

    AUTH_LDAP_SERVER_URI = get_secret('AUTH_LDAP_SERVER_URI')
    AUTH_LDAP_BIND_DN = get_secret('AUTH_LDAP_BIND_DN')
    AUTH_LDAP_BIND_PASSWORD = get_secret('AUTH_LDAP_BIND_PASSWORD')
    AUTH_LDAP_USER_SEARCH_ROOT = get_secret('AUTH_LDAP_USER_SEARCH_ROOT')
    AUTH_LDAP_GROUP_SEARCH_ROOT = get_secret('AUTH_LDAP_GROUP_SEARCH_ROOT')
    AUTH_LDAP_CHRIS_ADMIN_GROUP = get_secret('AUTH_LDAP_CHRIS_ADMIN_GROUP')

    _user_search = get_secret('AUTH_LDAP_USER_SEARCH_FILTER', env.str,
                              default='(uid=%(user)s)')
    AUTH_LDAP_USER_SEARCH = LDAPSearch(AUTH_LDAP_USER_SEARCH_ROOT, ldap.SCOPE_SUBTREE,
                                       _user_search)
    AUTH_LDAP_USER_ATTR_MAP = get_secret(
        'AUTH_LDAP_USER_ATTR_MAP',
        env.dict,
        default={
            'first_name': 'givenName',
            'last_name': 'sn',
            'email': 'mail'
        }
    )
    _group_search = get_secret('AUTH_LDAP_GROUP_SEARCH_FILTER', env.str,
                               default='(objectClass=groupOfNames)')
    AUTH_LDAP_GROUP_SEARCH = LDAPSearch(AUTH_LDAP_GROUP_SEARCH_ROOT, ldap.SCOPE_SUBTREE,
                                        _group_search)
    AUTH_LDAP_GROUP_TYPE = GroupOfNamesType()
    AUTH_LDAP_USER_FLAGS_BY_GROUP = get_secret(
        'AUTH_LDAP_USER_FLAGS_BY_GROUP',
        env.dict,
        default={
            'is_staff': f'cn={AUTH_LDAP_CHRIS_ADMIN_GROUP},{AUTH_LDAP_GROUP_SEARCH_ROOT}'
        }
    )
    AUTH_LDAP_MIRROR_GROUPS_EXCEPT = get_secret('AUTH_LDAP_MIRROR_GROUPS_EXCEPT',
                                                env.list, default=['all_users'])

    AUTHENTICATION_BACKENDS = (
        'users.models.CustomLDAPBackend',
        'django.contrib.auth.backends.ModelBackend',
    )


# ENABLE/DISABLE USER CREATION THROUGH AN API ENDPOINT
DISABLE_USER_ACCOUNT_CREATION = get_secret('DISABLE_USER_ACCOUNT_CREATION', env.bool)
