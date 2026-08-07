from django.urls import path

from .views import (
    AfterburnerItemInlineCreateView,
    AfterburnerItemInlineUpdateView,
    AfterburnerItemCreateView,
    AfterburnerItemDeleteView,
    AfterburnerItemMoveView,
    AfterburnerItemReorderView,
    AfterburnerItemUpdateView,
    AfterburnerProfileCreateView,
    AfterburnerProfileDeleteView,
    AfterburnerProfileDetailView,
    AfterburnerProfileListView,
    AfterburnerProfileUpdateView,
    ScriptInputCreateView,
    ScriptInputDeleteView,
    ScriptInputMoveView,
    ScriptInputReorderView,
    ScriptInputUpdateView,
)

app_name = "afterburners"

urlpatterns = [
    path("", AfterburnerProfileListView.as_view(), name="profile-list"),
    path("new/", AfterburnerProfileCreateView.as_view(), name="profile-create"),
    path("<int:pk>/", AfterburnerProfileDetailView.as_view(), name="profile-detail"),
    path("<int:pk>/edit/", AfterburnerProfileUpdateView.as_view(), name="profile-edit"),
    path("<int:pk>/delete/", AfterburnerProfileDeleteView.as_view(), name="profile-delete"),
    path("<int:profile_id>/items/new/", AfterburnerItemCreateView.as_view(), name="item-create"),
    path("<int:profile_id>/items/inline-create/", AfterburnerItemInlineCreateView.as_view(), name="item-inline-create"),
    path("items/<int:pk>/inline-update/", AfterburnerItemInlineUpdateView.as_view(), name="item-inline-update"),
    path("items/<int:pk>/edit/", AfterburnerItemUpdateView.as_view(), name="item-edit"),
    path("items/<int:pk>/delete/", AfterburnerItemDeleteView.as_view(), name="item-delete"),
    path("items/<int:pk>/move/<str:direction>/", AfterburnerItemMoveView.as_view(), name="item-move"),
    path("<int:profile_id>/items/reorder/", AfterburnerItemReorderView.as_view(), name="item-reorder"),
    path("items/<int:item_id>/inputs/new/", ScriptInputCreateView.as_view(), name="input-create"),
    path("items/<int:item_id>/inputs/reorder/", ScriptInputReorderView.as_view(), name="input-reorder"),
    path("inputs/<int:pk>/edit/", ScriptInputUpdateView.as_view(), name="input-edit"),
    path("inputs/<int:pk>/delete/", ScriptInputDeleteView.as_view(), name="input-delete"),
    path("inputs/<int:pk>/move/<str:direction>/", ScriptInputMoveView.as_view(), name="input-move"),
]
