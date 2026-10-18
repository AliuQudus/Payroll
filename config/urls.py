from django.contrib import admin
from django.urls import include, path

from security.views import SecureLoginView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", SecureLoginView.as_view(), name="login"),     # before the include, so it wins
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("security.urls")),
    path("leave/", include("leave.urls")),
    path("hr/", include("hr.urls")),
    path("recruitment/", include("recruitment.urls")),
    path("salary/", include("salary.urls")),
    path("exits/", include("exits.urls")),
    path("attendance/", include("attendance.urls")),
    path("portal/", include("portal.urls")),
    path("remittances/", include("remittances.urls")),
    path("audit/", include("audit.urls")),
    path("", include("payroll.urls")),
]
