# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring, global-statement

import typing as t
from types import TracebackType

import asyncio
import logging
import random
from ssl import SSLContext
import threading

import httpx
from httpx_socks import AsyncProxyTransport
from python_socks import parse_proxy_url, ProxyConnectionError, ProxyTimeoutError, ProxyError

from searx import logger

CertTypes = str | tuple[str, str] | tuple[str, str, str]
SslContextKeyType = tuple[str | None, CertTypes | None, bool, bool]

logger = logger.getChild('searx.network.client')
LOOP: asyncio.AbstractEventLoop = None  # pyright: ignore[reportAssignmentType]

SSLCONTEXTS: dict[SslContextKeyType, SSLContext] = {}


def shuffle_ciphers(ssl_context: SSLContext):
    """Shuffle httpx's default ciphers of a SSL context randomly."""
    c_list = [cipher["name"] for cipher in ssl_context.get_ciphers()]
    sc_list, c_list = c_list[:3], c_list[3:]
    random.shuffle(c_list)
    ssl_context.set_ciphers(":".join(sc_list + c_list))


def get_sslcontexts(
    proxy_url: str | None = None, cert: CertTypes | None = None, verify: bool = True, trust_env: bool = True
) -> SSLContext:
    key: SslContextKeyType = (proxy_url, cert, verify, trust_env)
    if key not in SSLCONTEXTS:
        SSLCONTEXTS[key] = httpx.create_ssl_context(verify, cert, trust_env)
    shuffle_ciphers(SSLCONTEXTS[key])
    return SSLCONTEXTS[key]


class AsyncHTTPTransportNoHttp(httpx.AsyncHTTPTransport):
    """Network transport that disables HTTP protocol, except for the sxng-proxy."""

    def __init__(self, *args, **kwargs):
        pass

    _proxy_transport = None

    async def handle_async_request(self, request: httpx.Request):
        # Whitelist sxng-proxy by name or containing string
        if 'sxng-proxy' in str(request.url.host):
            if AsyncHTTPTransportNoHttp._proxy_transport is None:
                AsyncHTTPTransportNoHttp._proxy_transport = httpx.AsyncHTTPTransport()
            return await AsyncHTTPTransportNoHttp._proxy_transport.handle_async_request(request)
        
        # Log which host was blocked for debugging
        logger.warning('Blocking HTTP request to: %s', request.url.host)
        raise httpx.UnsupportedProtocol(f'HTTP protocol is disabled for host: {request.url.host}')

    async def aclose(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        pass


class AsyncProxyTransportFixed(AsyncProxyTransport):
    """Fix httpx_socks.AsyncProxyTransport"""

    async def handle_async_request(self, request: httpx.Request):
        try:
            return await super().handle_async_request(request)
        except ProxyConnectionError as e:
            raise httpx.ProxyError("ProxyConnectionError: " + str(e.strerror), request=request) from e
        except ProxyTimeoutError as e:
            raise httpx.ProxyError("ProxyTimeoutError: " + str(e.args[0]), request=request) from e
        except ProxyError as e:
            raise httpx.ProxyError("ProxyError: " + str(e.args[0]), request=request) from e


def get_transport_for_socks_proxy(
    verify: bool, http2: bool, local_address: str, proxy_url: str, limit: httpx.Limits, retries: int
):
    rdns = False
    socks5h = 'socks5h://'
    if proxy_url.startswith(socks5h):
        proxy_url = 'socks5://' + proxy_url[len(socks5h) :]
        rdns = True

    proxy_type, proxy_host, proxy_port, proxy_username, proxy_password = parse_proxy_url(proxy_url)
    _verify = get_sslcontexts(proxy_url, None, verify, True) if verify is True else verify
    return AsyncProxyTransportFixed(
        proxy_type=proxy_type,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        username=proxy_username,
        password=proxy_password,
        rdns=rdns,
        loop=get_loop(),
        verify=_verify,  # pyright: ignore[reportArgumentType]
        http2=http2,
        local_address=local_address,
        limits=limit,
        retries=retries,
    )


def get_transport(
    verify: bool, http2: bool, local_address: str, proxy_url: str | None, limit: httpx.Limits, retries: int
):
    _verify = get_sslcontexts(None, None, verify, True) if verify is True else verify
    return httpx.AsyncHTTPTransport(
        verify=_verify,
        http2=http2,
        limits=limit,
        proxy=httpx._config.Proxy(proxy_url) if proxy_url else None,  # pyright: ignore[reportPrivateUsage]
        local_address=local_address,
        retries=retries,
    )


def new_client(
    enable_http: bool,
    verify: bool,
    enable_http2: bool,
    max_connections: int,
    max_keepalive_connections: int,
    keepalive_expiry: float,
    proxies: dict[str, str],
    local_address: str,
    retries: int,
    max_redirects: int,
    hook_log_response: t.Callable[..., t.Any] | None,
) -> httpx.AsyncClient:
    limit = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=keepalive_expiry,
    )
    mounts = {}
    for pattern, proxy_url in proxies.items():
        if not enable_http and pattern.startswith('http://'):
            continue
        if proxy_url.startswith('socks4://') or proxy_url.startswith('socks5://') or proxy_url.startswith('socks5h://'):
            mounts[pattern] = get_transport_for_socks_proxy(
                verify, enable_http2, local_address, proxy_url, limit, retries
            )
        else:
            mounts[pattern] = get_transport(verify, enable_http2, local_address, proxy_url, limit, retries)

    if not enable_http:
        mounts['http://'] = AsyncHTTPTransportNoHttp()

    transport = get_transport(verify, enable_http2, local_address, None, limit, retries)

    event_hooks = None
    if hook_log_response:
        event_hooks = {'response': [hook_log_response]}

    return httpx.AsyncClient(
        transport=transport,
        mounts=mounts,
        max_redirects=max_redirects,
        event_hooks=event_hooks,
    )


def get_loop() -> asyncio.AbstractEventLoop:
    return LOOP


def init():
    for logger_name in (
        'httpx',
        'httpcore.proxy',
        'httpcore.connection',
        'httpcore.http11',
        'httpcore.http2',
        'hpack.hpack',
        'hpack.table',
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    def loop_thread():
        global LOOP
        LOOP = asyncio.new_event_loop()
        LOOP.run_forever()

    thread = threading.Thread(
        target=loop_thread,
        name='asyncio_loop',
        daemon=True,
    )
    thread.start()


init()
