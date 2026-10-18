from .access import companies_for


class CompanyMiddleware:
    """Attaches the company the user is currently working in to every request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.company = None
        if request.user.is_authenticated:
            allowed = companies_for(request.user)
            cid = request.session.get("company_id")
            request.company = (allowed.filter(pk=cid).first() if cid else None) or allowed.order_by("pk").first()
        return self.get_response(request)
