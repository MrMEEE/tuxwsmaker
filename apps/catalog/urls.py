from django.urls import path

from .views import (
    ISODeleteView,
    ISOUploadView,
    ISOUpdateView,
    ISOVariableUpdateView,
    OSCreateView,
    OSDeleteView,
    OSDetailView,
    OSListView,
    OSUpdateView,
    OSVariableUpdateView,
)

app_name = "catalog"

urlpatterns = [
    path("", OSListView.as_view(), name="os-list"),
    path("new/", OSCreateView.as_view(), name="os-create"),
    path("<int:pk>/", OSDetailView.as_view(), name="os-detail"),
    path("<int:pk>/edit/", OSUpdateView.as_view(), name="os-edit"),
    path("<int:pk>/delete/", OSDeleteView.as_view(), name="os-delete"),
    path("<int:pk>/variables/", OSVariableUpdateView.as_view(), name="os-variables"),
    path("<int:pk>/isos/upload/", ISOUploadView.as_view(), name="iso-upload"),
    path("<int:pk>/isos/<int:iso_id>/edit/", ISOUpdateView.as_view(), name="iso-edit"),
    path("<int:pk>/isos/<int:iso_id>/delete/", ISODeleteView.as_view(), name="iso-delete"),
    path("<int:pk>/isos/<int:iso_id>/variables/", ISOVariableUpdateView.as_view(), name="iso-variables"),
]
