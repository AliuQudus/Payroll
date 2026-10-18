"""The QR code on the two-factor setup page. It needs the optional segno package; without it the page shows the
setup key to type in, which every authenticator app also accepts."""
from django.utils.safestring import mark_safe


def svg(text):
    try:
        import segno
    except ImportError:
        return None
    return mark_safe(segno.make(text, error="m").svg_inline(scale=5, border=2, dark="#000000", light="#ffffff"))
