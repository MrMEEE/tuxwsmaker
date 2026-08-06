from __future__ import annotations

from asgiref.sync import iscoroutinefunction, markcoroutinefunction


class BackfillCSRFMiddlewareTokenMiddleware:
    """Backfill missing csrfmiddlewaretoken from csrftoken cookie for POST forms.

    This keeps standard CSRF validation in place while avoiding intermittent
    missing-field issues caused by stale/dynamic client form state.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self._is_coroutine = iscoroutinefunction(get_response)
        if self._is_coroutine:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self._is_coroutine:
            return self.__acall__(request)

        self._backfill_token(request)
        return self.get_response(request)

    async def __acall__(self, request):
        self._backfill_token(request)
        return await self.get_response(request)

    def _backfill_token(self, request):
        if request.method == "POST":
            content_type = (request.META.get("CONTENT_TYPE") or "").lower()
            if content_type.startswith("multipart/form-data"):
                return

            submitted = request.POST.get("csrfmiddlewaretoken", "")
            cookie_token = request.COOKIES.get("csrftoken", "")
            if not submitted and cookie_token:
                mutable = request.POST.copy()
                mutable["csrfmiddlewaretoken"] = cookie_token
                request.POST = mutable
