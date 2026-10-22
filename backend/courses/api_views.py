from django.core import signing
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework import status

from catalog.models import Category, Product
from . import video_access
from .models import Course, CourseLesson, CoursePurchase
from .serializers import (
    CourseListSerializer,
    CourseDetailSerializer,
    LessonContentSerializer,
    MyCourseSerializer,
)


class CoursePagination(PageNumberPagination):
    page_size = 12
    page_size_query_param = 'page_size'
    max_page_size = 60


def _user_owns_course(user, course_id) -> bool:
    if not user or not user.is_authenticated:
        return False
    return CoursePurchase.objects.filter(user=user, course_id=course_id).exists()


@api_view(['GET'])
@permission_classes([AllowAny])
def courses_list(request):
    """Catalog of active courses. Optional filter ?category=slug."""
    qs = Course.objects.filter(is_active=True)
    category_slug = request.query_params.get('category')
    if category_slug:
        # Cascading: the subtree of the selected category to any depth
        try:
            cat = Category.objects.get(slug=category_slug)
            qs = qs.filter(related_categories__in=cat.get_subtree_ids_self()).distinct()
        except Category.DoesNotExist:
            qs = qs.none()
    qs = qs.order_by('sort_order', '-created_at')

    paginator = CoursePagination()
    page = paginator.paginate_queryset(qs, request)
    serializer = CourseListSerializer(page, many=True, context={'request': request})
    return paginator.get_paginated_response(serializer.data)


@api_view(['GET'])
@permission_classes([AllowAny])
def course_detail(request, slug):
    """Course detail: curriculum (without lesson content) + has_access flag."""
    try:
        course = Course.objects.prefetch_related(
            'modules__lessons', 'modules__children__lessons',
            'modules__children__children__lessons',
            'related_variants__images', 'related_variants__product__images',
            'related_bundles__images',
        ).get(slug=slug)
    except Course.DoesNotExist:
        return Response({'detail': 'Course not found'}, status=status.HTTP_404_NOT_FOUND)

    has_access = _user_owns_course(request.user, course.id)
    # is_active=False hides the course from the storefront, but does not take it away from those who
    # paid.
    if not course.is_active and not has_access:
        return Response({'detail': 'Course not found'}, status=status.HTTP_404_NOT_FOUND)
    serializer = CourseDetailSerializer(
        course,
        context={'request': request, 'has_access': has_access},
    )
    return Response(serializer.data)


@api_view(['GET'])
@permission_classes([AllowAny])
def lesson_content(request, slug, lesson_id):
    """Full lesson content. 403 if not purchased and not is_preview."""
    try:
        lesson = CourseLesson.objects.select_related('module__course').get(
            pk=lesson_id, module__course__slug=slug,
        )
    except CourseLesson.DoesNotExist:
        return Response({'detail': 'Lesson not found'}, status=status.HTTP_404_NOT_FOUND)

    owns = _user_owns_course(request.user, lesson.module.course_id)
    # A hidden course stays accessible to its buyers (see course_detail).
    if not lesson.module.course.is_active and not owns:
        return Response({'detail': 'Lesson not found'}, status=status.HTTP_404_NOT_FOUND)

    # A hidden lesson or section means 'taken off the storefront'. The buyer already has the
    # course, so we keep it for them, but we do not serve it to outsiders even with the 'preview'
    # flag:
    # otherwise a video taken off display would stay public.
    if (not lesson.is_active or not lesson.module.is_active) and not owns:
        return Response({'detail': 'Lesson not found'}, status=status.HTTP_404_NOT_FOUND)

    if not lesson.is_preview and not owns:
        return Response(
            {'detail': 'Access is only for those who bought the course'},
            status=status.HTTP_403_FORBIDDEN,
        )

    serializer = LessonContentSerializer(lesson, context={'request': request})
    return Response(serializer.data)


# HEAD is required: the player first probes the source with this method (checks
# type, size and Range support) — on a 405 it considers the video unusable and
# shows a black screen without even trying GET.
@api_view(['GET', 'HEAD'])
@permission_classes([AllowAny])
# No throttling: the browser fetches video in ranges (Range), and a single seek produces
# dozens of requests from one IP. The shared anonymous limit of 120/min knocked the viewer
# out with a 429 mid-lesson. Access here is guarded by the signature + the CoursePurchase check.
@throttle_classes([])
def course_video(request, token):
    """Serves paid video via a signed link (see courses/video_access.py).

    AllowAny on purpose: `<video src>` does not send Authorization, so the signature itself is
    the proof of entitlement. But we check the entitlement AGAIN against CoursePurchase — the token
    may
    have been issued before the purchase was rolled back.
    """
    try:
        data = signing.loads(token, salt=video_access.SALT, max_age=video_access.TTL_SECONDS)
    except signing.SignatureExpired:
        return Response({'detail': 'The link has expired — refresh the page'},
                        status=status.HTTP_410_GONE)
    except signing.BadSignature:
        return Response({'detail': 'Invalid link'}, status=status.HTTP_403_FORBIDDEN)

    kind, obj_id, user_id = data.get('k'), data.get('i'), data.get('u')

    # The link was issued to a specific buyer. If a DIFFERENT logged-in user opens it, we
    # refuse: otherwise a forwarded link would work for anyone until it expires. We do not touch
    # anonymous users: they simply have no purchase, and below they will only get through to a
    # preview lesson.
    if (user_id and request.user.is_authenticated and request.user.id != user_id):
        return Response({'detail': 'The link was issued to another user'},
                        status=status.HTTP_403_FORBIDDEN)

    if kind == video_access.KIND_LESSON:
        lesson = CourseLesson.objects.select_related('module__course').filter(pk=obj_id).first()
        if lesson is None or not lesson.video_file:
            return Response({'detail': 'Video not found'}, status=status.HTTP_404_NOT_FOUND)
        # A preview lesson is watched without a purchase — it is the course's storefront.
        if not lesson.is_preview and not _purchase_exists(user_id, lesson.module.course_id):
            return Response({'detail': 'Access is only for those who bought the course'},
                            status=status.HTTP_403_FORBIDDEN)
        return video_access.serve(lesson.video_file, request)

    if kind == video_access.KIND_FULL:
        course = Course.objects.filter(pk=obj_id).first()
        if course is None or not course.full_video_file:
            return Response({'detail': 'Video not found'}, status=status.HTTP_404_NOT_FOUND)
        if not _purchase_exists(user_id, course.id):
            return Response({'detail': 'Access is only for those who bought the course'},
                            status=status.HTTP_403_FORBIDDEN)
        return video_access.serve(course.full_video_file, request)

    return Response({'detail': 'Invalid link'}, status=status.HTTP_403_FORBIDDEN)


def _purchase_exists(user_id, course_id) -> bool:
    if not user_id:
        return False
    return CoursePurchase.objects.filter(user_id=user_id, course_id=course_id).exists()


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_courses(request):
    """Purchased courses of the current user + pending orders with courses.

    `pending`: orders with payment_status='pending' that contain a course the user has not
    received yet (not in CoursePurchase). Lets /my-courses show a badge
    "Awaiting payment confirmation #ORDER".
    """
    purchased = CoursePurchase.objects.filter(user=request.user).select_related('course').order_by('-purchased_at')
    purchased_data = MyCourseSerializer(purchased, many=True, context={'request': request}).data

    # Pending: the user's OrderItems with a course in orders that are not paid yet and have no
    # CoursePurchase yet.
    from orders.models import OrderItem
    owned_course_ids = set(purchased.values_list('course_id', flat=True))
    pending_items = OrderItem.objects.filter(
        order__user=request.user,
        course__isnull=False,
        order__payment_status='pending',
    ).exclude(course_id__in=owned_course_ids).select_related('order', 'course').order_by('-order__created_at')

    pending_data = []
    seen = set()
    for item in pending_items:
        if item.course_id in seen:
            continue
        seen.add(item.course_id)
        pending_data.append({
            'course': {
                'id': item.course.id,
                'name': item.course.name,
                'slug': item.course.slug,
                'cover_url': request.build_absolute_uri(item.course.cover_image.url) if item.course.cover_image else None,
                'price': str(item.course.price),
            },
            'order_number': item.order.order_number,
            'order_payment_method': item.order.payment_method,
            'created_at': item.order.created_at,
        })

    return Response({'purchased': purchased_data, 'pending': pending_data})


@api_view(['GET'])
@permission_classes([AllowAny])
def product_courses(request, slug):
    """Courses linked to a product (cross-sell on the product page)."""
    try:
        product = Product.objects.get(slug=slug, is_active=True)
    except Product.DoesNotExist:
        return Response([])

    # A course now references a PACKAGING (related_variants), not a product, so the reverse
    # `product.related_courses` no longer exists — we go through the product's packagings.
    # distinct(): a course may hold several packagings of the same product.
    courses = (Course.objects.filter(related_variants__product=product, is_active=True)
               .distinct().order_by('sort_order', '-created_at'))
    serializer = CourseListSerializer(courses, many=True, context={'request': request})
    return Response(serializer.data)
