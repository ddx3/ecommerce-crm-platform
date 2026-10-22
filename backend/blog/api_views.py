from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import BlogPost
from .serializers import BlogPostListSerializer, BlogPostDetailSerializer


class BlogPagination(PageNumberPagination):
    page_size = 12
    page_size_query_param = 'page_size'
    max_page_size = 500


@api_view(['GET'])
@permission_classes([AllowAny])
def posts_list(request):
    qs = BlogPost.objects.filter(is_active=True)
    limit_param = request.GET.get('limit')
    if limit_param:
        try:
            n = max(1, min(int(limit_param), 500))
            data = BlogPostListSerializer(qs[:n], many=True, context={'request': request}).data
            return Response({'results': data, 'count': qs.count()})
        except ValueError:
            pass
    paginator = BlogPagination()
    page = paginator.paginate_queryset(qs, request)
    data = BlogPostListSerializer(page, many=True, context={'request': request}).data
    return paginator.get_paginated_response(data)


@api_view(['GET'])
@permission_classes([AllowAny])
def post_detail(request, slug):
    post = get_object_or_404(BlogPost, slug=slug, is_active=True)
    data = BlogPostDetailSerializer(post, context={'request': request}).data
    return Response(data)
