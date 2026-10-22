import re

from rest_framework import serializers

from .models import BlogPost


def _strip_html(html: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', html or '')
    return re.sub(r'\s+', ' ', text).strip()


class BlogPostListSerializer(serializers.ModelSerializer):
    preview_image = serializers.SerializerMethodField()
    excerpt = serializers.SerializerMethodField()

    class Meta:
        model = BlogPost
        fields = ['id', 'slug', 'title', 'preview_image', 'excerpt', 'created_at']

    def get_preview_image(self, obj):
        if not obj.preview_image:
            return None
        request = self.context.get('request')
        url = obj.preview_image.url
        return request.build_absolute_uri(url) if request else url

    def get_excerpt(self, obj):
        text = _strip_html(obj.content)
        return text[:200] + ('…' if len(text) > 200 else '')


class BlogPostDetailSerializer(BlogPostListSerializer):
    class Meta(BlogPostListSerializer.Meta):
        fields = BlogPostListSerializer.Meta.fields + [
            'content', 'youtube_url', 'updated_at', 'meta_title', 'meta_description',
        ]
