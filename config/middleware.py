from __future__ import annotations


class BackfillCSRFMiddlewareTokenMiddleware:
    """Backfill missing csrfmiddlewaretoken from csrftoken cookie for POST forms.

    This keeps standard CSRF validation in place while avoiding intermittent
    missing-field issues caused by stale/dynamic client form state.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST":
            content_type = (request.META.get("CONTENT_TYPE") or "").lower()
            if content_type.startswith("multipart/form-data"):
                return self.get_response(request)

            submitted = request.POST.get("csrfmiddlewaretoken", "")
            cookie_token = request.COOKIES.get("csrftoken", "")
            if not submitted and cookie_token:
                mutable = request.POST.copy()
                mutable["csrfmiddlewaretoken"] = cookie_token
                request.POST = mutable
        return self.get_response(request)
