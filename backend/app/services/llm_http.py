from __future__ import annotations

import ipaddress
from urllib.request import getproxies

import httpx


_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _ip_address(host: str) -> _IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _no_proxy_matches(host: str, port: int, no_proxy: str) -> bool:
    """Match bypass rules locally; CIDRs apply only to literal IP targets."""
    host = host.lower().rstrip(".")
    target_ip = _ip_address(host)
    for rule in no_proxy.split(","):
        rule = rule.strip()
        if not rule:
            continue
        if rule == "*":
            return True
        if "/" in rule:
            try:
                network = ipaddress.ip_network(rule, strict=False)
            except ValueError:
                continue
            if target_ip is not None and target_ip in network:
                return True
            continue

        # An unbracketed IPv6 literal has colons but no port. IPv6 host:port
        # rules must use brackets, just like URLs, to avoid that ambiguity.
        rule_host = rule
        rule_port = None
        if rule.startswith("["):
            closing = rule.find("]")
            if closing == -1:
                continue
            rule_host = rule[1:closing]
            suffix = rule[closing + 1:]
            if suffix:
                if not suffix.startswith(":") or not suffix[1:].isdigit():
                    continue
                rule_port = int(suffix[1:])
        elif rule.count(":") == 1:
            rule_host, port_text = rule.rsplit(":", 1)
            if not port_text.isdigit():
                continue
            rule_port = int(port_text)
        if rule_port is not None and rule_port != port:
            continue

        rule_host = rule_host.lower().rstrip(".")
        rule_ip = _ip_address(rule_host)
        if rule_ip is not None:
            if target_ip == rule_ip:
                return True
            continue
        # Never treat an IP rule as a DNS suffix, or resolve a hostname to
        # decide whether it belongs to an IP network.
        if target_ip is not None or ":" in rule_host:
            continue
        suffix = rule_host.lstrip(".")
        if suffix and (host == suffix or host.endswith("." + suffix)):
            return True
    return False


def select_environment_proxy(url: str) -> str | None:
    """Select one proxy without httpx's NO_PROXY-to-URLPattern conversion.

    getproxies preserves urllib's lowercase-variable precedence and CGI
    HTTP_PROXY protection. HTTP(S) overrides ALL_PROXY only for its scheme.
    TLS environment handling remains the responsibility of HTTPTransport.
    """
    target = httpx.URL(url)
    if target.scheme not in ("http", "https") or not target.host:
        raise ValueError("LLM endpoint must be an absolute HTTP(S) URL")
    proxies = getproxies()
    port = target.port if target.port is not None else (443 if target.scheme == "https" else 80)
    if _no_proxy_matches(target.host, port, proxies.get("no", "")):
        return None
    proxy = proxies.get(target.scheme) or proxies.get("all")
    if not proxy:
        return None
    # httpx's environment-proxy path also defaults a scheme-less value to HTTP.
    proxy = proxy if "://" in proxy else f"http://{proxy}"
    proxy_url = httpx.URL(proxy)
    if proxy_url.scheme not in ("http", "https", "socks5", "socks5h") or not proxy_url.host:
        raise ValueError("Invalid environment proxy URL")
    return proxy
