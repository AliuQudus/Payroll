"""The visitor's address, as far as it can be trusted."""
import ipaddress

from django.conf import settings


def client_ip(request):
    """REMOTE_ADDR, or the header named in CLIENT_IP_HEADER when a trusted proxy supplies the real address.

    A header the visitor can set themselves must never be trusted, so the header is used only when configured.
    For a list such as X-Forwarded-For, the last entry is the one added by your own proxy.
    """
    if request is None:
        return None
    header = getattr(settings, "CLIENT_IP_HEADER", None)
    if header:
        value = request.META.get(header, "").split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass                                    # missing or not an address: fall back rather than trust it
    return request.META.get("REMOTE_ADDR") or None
