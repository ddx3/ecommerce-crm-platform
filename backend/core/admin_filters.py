from django.contrib import admin


class IsActiveFilter(admin.SimpleListFilter):
    """Unified display filter (is_active): Active / Inactive."""

    title = 'Shown'
    parameter_name = 'is_active'

    def lookups(self, request, model_admin):
        return [('1', 'Active'), ('0', 'Inactive')]

    def queryset(self, request, queryset):
        value = self.value()
        if value == '1':
            return queryset.filter(is_active=True)
        if value == '0':
            return queryset.filter(is_active=False)
        return queryset
