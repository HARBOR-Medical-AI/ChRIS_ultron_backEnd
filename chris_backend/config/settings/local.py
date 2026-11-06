# -*- coding: utf-8 -*-
"""
Local settings

- Run in Debug mode
- Use console backend for emails
- Add Django Debug Toolbar
- Add django-extensions as app
"""

import os
import ldap
from django_auth_ldap.config import LDAPSearch, GroupOfNamesType
from .common import *  # noqa
from core.storage import verify_storage_connection
from core.cache import verify_cache_connection

# Normally you should not import ANYTHING from Django directly
# into your settings, but ImproperlyConfigured is an exception.
from django.core.exceptions import ImproperlyConfigured

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# as for urls for dev env
ROOT_URLCONF = 'config.local_urls'

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'w1kxu^l=@pnsf!5piqz6!!5kdcdpo79y6jebbp+2244yjm*#+k'

# Superuser settings
CHRIS_SUPERUSER_PASSWORD = 'chris1234'

# Hosts/domain names that are valid for this site
# See https://docs.djangoproject.com/en/6.0/ref/settings/#allowed-hosts
ALLOWED_HOSTS = ['*']

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

# LOGGING CONFIGURATION
# See https://docs.djangoproject.com/en/6.0/topics/logging/ for
# more details on how to customize your logging configuration.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[%(asctime)s] [%(levelname)s]'
                      '[%(name)s][%(filename)s:%(lineno)d %(funcName)s] %(message)s'
        },
        'simple': {
            'format': '[%(asctime)s] [%(levelname)s]'
                      '[%(module)s %(process)d %(thread)d] %(message)s'
        },
    },
    'handlers': {
        'console_verbose': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'console_simple': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
        'file': {
            'level': 'DEBUG',
            'class': 'logging.FileHandler',
            'filename': '/tmp/debug.log',
            'formatter': 'simple'
        }
    },
    'loggers': {
        '': {  # root logger
            'level': 'INFO',
            'handlers': ['console_simple'],
        },
    }
}

for app in ['collectionjson', 'core', 'feeds', 'plugins', 'plugininstances',
            'pipelines', 'userfiles', 'pacsfiles', 'users', 'filebrowser',
            'workflows']:
    LOGGING['loggers'][app] = {
        'level': 'DEBUG',
        'handlers': ['console_verbose', 'file'],
        'propagate': False  # required to avoid double logging with root logger
    }

# Storage Settings
STORAGE_ENV = os.getenv('STORAGE_ENV', 'swift')
if STORAGE_ENV not in ('swift', 'fslink', 'filesystem', 's3'):
    raise ImproperlyConfigured(f"Unsupported value '{STORAGE_ENV}' for STORAGE_ENV")

STORAGES['default'] = {'BACKEND': 'swift.storage.SwiftStorage'}
SWIFT_AUTH_URL = 'http://swift_service:8080/auth/v1.0'  # Swift service settings
SWIFT_USERNAME = 'chris:chris1234'
SWIFT_KEY = 'testing'
SWIFT_CONTAINER_NAME = 'users'
SWIFT_CONNECTION_PARAMS = {'user': SWIFT_USERNAME,
                           'key': SWIFT_KEY,
                           'authurl': SWIFT_AUTH_URL}
MEDIA_ROOT = None
S3_BUCKET_NAME = None
S3_CONNECTION_PARAMS = None

if STORAGE_ENV in ('fslink', 'filesystem'):
    STORAGES['default'] = {'BACKEND': 'django.core.files.storage.FileSystemStorage'}
    MEDIA_ROOT = '/data'  # local filesystem storage settings
elif STORAGE_ENV == 's3':
    STORAGES['default'] = {'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage'}
    S3_BUCKET_NAME = os.getenv('S3_BUCKET_NAME', 'users')
    S3_CONNECTION_PARAMS = {
        'endpoint_url': os.getenv('S3_ENDPOINT_URL', 'http://s3-service:9000'),
        'access_key': os.getenv('S3_ACCESS_KEY', 'minioadmin'),
        'secret_key': os.getenv('S3_SECRET_KEY', 'minioadmin'),
        'region_name': os.getenv('S3_REGION', None),
    }
    # django-storages S3 settings (derived from ChRIS S3 config above)
    AWS_STORAGE_BUCKET_NAME = S3_BUCKET_NAME
    AWS_S3_ENDPOINT_URL = S3_CONNECTION_PARAMS['endpoint_url']
    AWS_ACCESS_KEY_ID = S3_CONNECTION_PARAMS['access_key']
    AWS_SECRET_ACCESS_KEY = S3_CONNECTION_PARAMS['secret_key']
    AWS_S3_REGION_NAME = S3_CONNECTION_PARAMS.get('region_name') or 'us-east-1'
    AWS_S3_ADDRESSING_STYLE = 'path'
    AWS_S3_SIGNATURE_VERSION = 's3v4'

try:
    verify_storage_connection(
        STORAGES=STORAGES,
        MEDIA_ROOT=MEDIA_ROOT,
        SWIFT_CONTAINER_NAME=SWIFT_CONTAINER_NAME,
        SWIFT_CONNECTION_PARAMS=SWIFT_CONNECTION_PARAMS,
        S3_BUCKET_NAME=S3_BUCKET_NAME,
        S3_CONNECTION_PARAMS=S3_CONNECTION_PARAMS,
    )
except Exception as e:
    raise ImproperlyConfigured(str(e))

# ChRIS store settings
CHRIS_STORE_URL = 'http://chris-store.local:8010/api/v1/'

# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases
DATABASES['default']['NAME'] = 'chris_dev'
DATABASES['default']['USER'] = 'chris'
DATABASES['default']['PASSWORD'] = 'Chris1234'
DATABASES['default']['TEST'] = {'NAME': 'test_chris_dev'}
DATABASES['default']['HOST'] = 'chris_dev_db'
DATABASES['default']['PORT'] = '5432'
DATABASES['default']['OPTIONS'] = {'pool': {'min_size': 1, 'max_size': 2, 'timeout': 10}}
# Apply the QIDO-RS fuzzy-matching similarity threshold GUC (see common.py),
# whether or not the connection pool branch above ran.
apply_dicomweb_fuzzy_threshold(DATABASES['default'])  # noqa: F405

# Mail settings
# ------------------------------------------------------------------------------
EMAIL_HOST = 'localhost'
EMAIL_PORT = 1025
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# django-debug-toolbar
# ------------------------------------------------------------------------------
MIDDLEWARE += ['debug_toolbar.middleware.DebugToolbarMiddleware']
INSTALLED_APPS += ['debug_toolbar']

INTERNAL_IPS = ['127.0.0.1',]

# No DEBUG_TOOLBAR_CONFIG: the defaults are what we want.

# django-extensions
# ------------------------------------------------------------------------------
INSTALLED_APPS += ['django_extensions']

# TESTING
# ------------------------------------------------------------------------------
TEST_RUNNER = 'core.testrunner.TerminateConnectionsTestRunner'

COMPUTE_RESOURCE_URL = 'http://pfcon.remote:30005/api/v1/'


# corsheaders
# ------------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = True
CORS_EXPOSE_HEADERS = ['Allow', 'Content-Type', 'Content-Length']


# NATS settings
NATS_ADDRESS = 'nats://nats:4222'

# PFDCM settings
PFDCM_ADDRESS = 'http://pfdcm:4005'


# Celery settings

CELERY_BROKER_URL = 'redis://chris-broker:6379/0'

#: Only add pickle to this list if your broker is secured
#: from unwanted access (see userguide/security.html)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'

# Worker settings
# messages to prefetch at a time multiplied by the number of concurrent processes
# default is 4 (four messages for each process)
CELERYD_PREFETCH_MULTIPLIER = 2


# Cache settings (see common.py)
# The benchmark stack records CUBE_CACHE_URL in each run's environment manifest.
CACHES['default']['LOCATION'] = os.getenv('CUBE_CACHE_URL',  # noqa: F405
                                          'redis://chris-cache:6379/0')
verify_cache_connection(CACHES['default'], CELERY_BROKER_URL)  # noqa: F405


# LDAP auth configuration
AUTH_LDAP = True
if AUTH_LDAP:
    AUTH_LDAP_SERVER_URI = 'ldap://lldap:3890'
    AUTH_LDAP_BIND_DN = 'uid=admin,ou=people,dc=example,dc=org'
    AUTH_LDAP_BIND_PASSWORD = 'chris1234'
    AUTH_LDAP_USER_SEARCH_ROOT = 'ou=people,dc=example,dc=org'
    AUTH_LDAP_GROUP_SEARCH_ROOT = 'ou=groups,dc=example,dc=org'
    AUTH_LDAP_CHRIS_ADMIN_GROUP = 'chris_admin'

    AUTH_LDAP_USER_SEARCH = LDAPSearch(AUTH_LDAP_USER_SEARCH_ROOT, ldap.SCOPE_SUBTREE,
                                       '(uid=%(user)s)')
    AUTH_LDAP_USER_ATTR_MAP = {
        'first_name': 'givenName',
        'last_name': 'sn',
        'email': 'mail'
    }
    AUTH_LDAP_GROUP_SEARCH = LDAPSearch(AUTH_LDAP_GROUP_SEARCH_ROOT, ldap.SCOPE_SUBTREE,
                                        '(objectClass=groupOfNames)')
    AUTH_LDAP_GROUP_TYPE = GroupOfNamesType()
    AUTH_LDAP_USER_FLAGS_BY_GROUP = {
        'is_staff': f'cn={AUTH_LDAP_CHRIS_ADMIN_GROUP},{AUTH_LDAP_GROUP_SEARCH_ROOT}'
    }
    AUTH_LDAP_MIRROR_GROUPS_EXCEPT = ['all_users']

    AUTHENTICATION_BACKENDS = (
        'users.models.CustomLDAPBackend',
        'django.contrib.auth.backends.ModelBackend',
    )


# Setting to enable/disable user creation through an API endpoint
DISABLE_USER_ACCOUNT_CREATION = False
