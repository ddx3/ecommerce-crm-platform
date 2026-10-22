from django import forms
from django.contrib import admin
from django.utils import timezone
from django.utils.safestring import mark_safe

from catalog.widgets import QuillWidget
from core.admin_filters import IsActiveFilter
from .models import BlogPost


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ['preview_thumb', 'title', 'is_active', 'created_display']
    list_display_links = ['title']
    list_editable = ['is_active']
    list_filter = [IsActiveFilter, 'created_at']
    search_fields = ['title']
    change_form_template = 'admin/blog/blogpost/change_form.html'
    change_list_template = 'admin/blog/blogpost/change_list.html'
    # Everything on one page — pagination and filters are client-side (JS).
    list_per_page = 10000
    save_on_top = True
    fieldsets = [
        (None, {'fields': ['youtube_url', 'title', 'is_active', 'preview_image', 'content']}),
    ]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields['content'].widget = QuillWidget(label='Content')
        # Plain URL input — without the "Currently / Change" rendering of
        # the default URLInput in Django 5+
        form.base_fields['youtube_url'].widget = forms.TextInput(attrs={'class': 'vURLField', 'maxlength': '500'})
        return form

    @admin.display(description='Created', ordering='created_at')
    def created_display(self, obj):
        dt = timezone.localtime(obj.created_at)
        return mark_safe(f'<span data-iso="{dt:%Y-%m-%d}">{dt:%d.%m.%Y %H:%M}</span>')

    @admin.display(description='Preview')
    def preview_thumb(self, obj):
        if obj.preview_image:
            url = obj.preview_image.url
            return mark_safe(
                f'<span class="bp-photo-wrap">'
                f'<img src="{url}" class="bp-thumb">'
                f'<span class="bp-photo-preview"><img src="{url}"></span>'
                f'</span>'
            )
        return mark_safe('<span class="bp-thumb-empty"></span>')
