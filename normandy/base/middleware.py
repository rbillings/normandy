import datetime
import logging
import os
import re

from django import http
from django.conf import settings
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.utils.deprecation import MiddlewareMixin
from django.contrib.auth.middleware import RemoteUserMiddleware
from django.middleware.common import CommonMiddleware
from django.middleware.security import SecurityMiddleware

from mozilla_cloud_services_logger.django.middleware import (
    RequestSummaryLogger as OriginalRequestSummaryLogger
)
from whitenoise.middleware import WhiteNoiseMiddleware


DEBUG_HTTP_TO_HTTPS_REDIRECT = 'normandy.base.middleware.D001'


logger = logging.getLogger(__name__)


def request_received_at_middleware(get_response):
    """
    Adds a 'received_at' property to requests with a datetime showing
    when the request was received by Django.
    """

    def middleware(request):
        request.received_at = timezone.now()
        return get_response(request)

    return middleware


class RequestSummaryLogger(MiddlewareMixin, OriginalRequestSummaryLogger):
    """
    Adapt mozilla_cloud_services_logger's request logger to Django 1.10 new-style middleware.
    """

    # TODO: Remove this once it is fixed upstream
    def _build_extra_meta(self, request):
        out = {
            "errno": 0,
            "agent": request.META.get('HTTP_USER_AGENT', ''),
            "lang": request.META.get('HTTP_ACCEPT_LANGUAGE', ''),
            "method": request.method,
            "path": request.path,
        }

        # HACK: It's possible some other middleware has replaced the request we
        # modified earlier, so be sure to check for existence of these
        # attributes before trying to use them.
        if hasattr(request, 'user'):
            out['uid'] = (request.user.is_authenticated and
                          request.user.id or '')
        if hasattr(request, '_id'):
            out['rid'] = request._id
        if hasattr(request, '_logging_start_dt'):
            td = datetime.datetime.utcnow() - request._logging_start_dt
            out['t'] = int(td.total_seconds() * 1000)  # in ms

        return out


class ConfigurableRemoteUserMiddleware(RemoteUserMiddleware):
    """
    Makes RemoteUserMiddleware customizable via settings.
    """

    @property
    def header(self):
        """
        Name of request header to grab username from.

        This will be the key as used in the request.META dictionary. To
        reference HTTP headers, this value should be all upper case and
        prefixed with "HTTP_".
        """
        return settings.OIDC_REMOTE_AUTH_HEADER


class NormandyWhiteNoiseMiddleware(WhiteNoiseMiddleware):

    def is_immutable_file(self, path, url):
        """
        Determine whether given URL represents an immutable file (i.e. a
        file with a hash of its contents as part of its name) which can
        therefore be cached forever
        """
        if not url.startswith(self.static_prefix):
            return False
        filename = os.path.basename(url)
        # Check if the filename ends with either 20 or 32 hex digits, and then an extension
        # 20 is for normal hashed content, like JS or CSS files, which use "[name].[hash].[ext]"
        # 32 is for content addressed files, like images or fonts, which use "[hash].[ext]"
        match = re.match(r'^.*([a-f0-9]{20}|[a-f0-9]{32})\.[\w\d]+$', filename)
        return bool(match)


class HttpResponsePermanentRedirectCached(http.HttpResponsePermanentRedirect):
    """
    Permanent redirect that also includes cache headers.

    This primarily helps our infrastructure stay reliable and
    performant, and softens "permanent" redirects to merely be long
    lived, which helps with maintainability.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        patch_cache_control(self, public=True, max_age=settings.PERMANENT_REDIRECT_CACHE_TIME)


class NormandyCommonMiddleware(CommonMiddleware):
    """
    Overrides CommonMiddleware to customize it to Normandy's needs.

    Includes cache control headers on APPEND_SLASH redirects.
    """

    response_redirect_class = HttpResponsePermanentRedirectCached


class NormandySecurityMiddleware(SecurityMiddleware):
    """Logs HTTP to HTTPS redirects, and adds cache headers to them."""

    def process_request(self, request):
        response = super().process_request(request)
        if response is not None:
            assert type(response) is http.HttpResponsePermanentRedirect
            patch_cache_control(response, public=True, max_age=settings.HTTPS_REDIRECT_CACHE_TIME)

            # Pull out just the HTTP headers from the rest of the request meta
            headers = {
                key.lstrip('HTTP_'): value
                for (key, value) in request.META.items()
                if key.startswith('HTTP_') or key == 'CONTENT_TYPE' or key == 'CONTENT_LENGTH'
            }

            logger.debug(
                f'Served HTTP to HTTPS redirect for {request.path}',
                extra={
                    'code': DEBUG_HTTP_TO_HTTPS_REDIRECT,
                    'method': request.method,
                    'body': request.body.decode('utf-8'),
                    'path': request.path,
                    'headers': headers,
                }
            )
        return response
