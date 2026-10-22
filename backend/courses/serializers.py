import html as html_lib
import re

from rest_framework import serializers

from . import video_access
from .models import Course, CourseModule, CourseLesson, CoursePurchase


def _abs(request, fileurl):
    if not fileurl:
        return None
    if not request:
        return fileurl
    return request.build_absolute_uri(fileurl)


def _teaser(html, limit=200):
    """Short text for the catalog card — taken from the full description.

    There is no separate "Short description" field in the form: a course has one
    description, so we cut it — without tags, at a word boundary.
    """
    text = re.sub(r'<[^>]+>', ' ', html or '')
    text = html_lib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(' ')
    return (cut[:sp] if sp > limit * 0.6 else cut).rstrip(' ,.;:') + '…'


def _viewer_id(request):
    user = getattr(request, 'user', None)
    return user.id if (user and user.is_authenticated) else None


class CourseListSerializer(serializers.ModelSerializer):
    """Course card for the catalog."""
    cover_url = serializers.SerializerMethodField()
    short_description = serializers.SerializerMethodField()
    total_lessons = serializers.IntegerField(read_only=True)
    total_duration_minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = Course
        fields = [
            'id', 'name', 'slug', 'short_description', 'cover_url',
            'instructor_name', 'price', 'old_price',
            'total_lessons', 'total_duration_minutes',
        ]

    def get_cover_url(self, obj):
        return _abs(self.context.get('request'), obj.cover_image.url if obj.cover_image else None)

    def get_short_description(self, obj):
        return _teaser(obj.description)


class CourseLessonProgramSerializer(serializers.ModelSerializer):
    """A lesson in the course curriculum — WITHOUT content (only title, type, duration)."""
    poster_url = serializers.SerializerMethodField()

    class Meta:
        model = CourseLesson
        fields = ['id', 'title', 'lesson_type', 'duration_minutes',
                  'is_preview', 'sort_order', 'poster_url']

    def get_poster_url(self, obj):
        return _abs(self.context.get('request'), obj.poster.url if obj.poster else None)


class CourseModuleProgramSerializer(serializers.ModelSerializer):
    lessons = serializers.SerializerMethodField()
    children = serializers.SerializerMethodField()
    # A lesson and a module are one node with one name: the node's own lesson (the one named
    # after the module) is returned separately and NOT duplicated in the lessons list. If there is
    # nothing besides it, the node is shown as a single lesson row (is_leaf).
    is_leaf = serializers.SerializerMethodField()
    own_lesson = serializers.SerializerMethodField()

    class Meta:
        model = CourseModule
        fields = ['id', 'title', 'sort_order', 'lessons', 'children',
                  'is_leaf', 'own_lesson']

    def _own(self, obj):
        own = obj.own_lesson()
        return own if (own and own.is_active) else None

    def _other_lessons(self, obj):
        own = obj.own_lesson()
        own_pk = own.pk if own else None
        return sorted([l for l in obj.lessons.all() if l.is_active and l.pk != own_pk],
                      key=lambda l: (l.sort_order, l.id))

    def _active_children(self, obj):
        return sorted([m for m in obj.children.all() if m.is_active],
                      key=lambda m: (m.sort_order, m.id))

    def get_is_leaf(self, obj):
        return bool(self._own(obj)) and not self._active_children(obj) \
            and not self._other_lessons(obj)

    def get_own_lesson(self, obj):
        own = self._own(obj)
        return CourseLessonProgramSerializer(own, context=self.context).data if own else None

    def get_children(self, obj):
        # Modules are nested (1 → 1.1 → 1.1.1) — we return a tree, not a flat list.
        # Hidden ones (is_active=False) do not reach the storefront together with their subtree.
        return CourseModuleProgramSerializer(
            self._active_children(obj), many=True, context=self.context).data

    def get_lessons(self, obj):
        return CourseLessonProgramSerializer(
            self._other_lessons(obj), many=True, context=self.context).data


class CourseDetailSerializer(serializers.ModelSerializer):
    """Course detail: full description + curriculum (modules/lessons without content).
    The `has_access` field is computed in the view and passed via context."""
    cover_url = serializers.SerializerMethodField()
    short_description = serializers.SerializerMethodField()
    modules = serializers.SerializerMethodField()
    total_lessons = serializers.IntegerField(read_only=True)
    total_duration_minutes = serializers.IntegerField(read_only=True)
    has_access = serializers.SerializerMethodField()
    related_products = serializers.SerializerMethodField()
    related_bundles = serializers.SerializerMethodField()
    full_video_url = serializers.SerializerMethodField()
    full_video_poster_url = serializers.SerializerMethodField()
    # Only the buyer sees the link itself, but a guest must also know that the full video EXISTS —
    # otherwise the "Full course video" block would be shown to everyone
    # (with the course cover as its poster, which always exists).
    has_full_video = serializers.SerializerMethodField()

    class Meta:
        model = Course
        fields = [
            'id', 'name', 'slug', 'short_description', 'description', 'cover_url',
            'instructor_name', 'price', 'old_price',
            'total_lessons', 'total_duration_minutes',
            'meta_title', 'meta_description',
            'modules', 'has_access', 'related_products', 'related_bundles',
            'full_video_url', 'full_video_poster_url', 'has_full_video', 'full_video_chapters',
        ]

    def get_cover_url(self, obj):
        return _abs(self.context.get('request'), obj.cover_image.url if obj.cover_image else None)

    def get_short_description(self, obj):
        return _teaser(obj.description)

    def get_has_access(self, obj):
        return bool(self.context.get('has_access'))

    def get_has_full_video(self, obj):
        return bool(obj.full_video_file)

    def get_full_video_url(self, obj):
        # The full video is paid content: we give the URL only to those who bought the course.
        # Not a raw MEDIA path but a signed link — nginx does not serve the file directly
        # (see courses/video_access.py).
        if not obj.full_video_file or not self.context.get('has_access'):
            return None
        request = self.context.get('request')
        return video_access.build_url(
            request, video_access.KIND_FULL, obj.id, _viewer_id(request),
        )

    def get_full_video_poster_url(self, obj):
        # A course has one photo: the cover. It is also the poster of the full video — there is no
        # separate poster in the form anymore (the model field remains, with old files).
        return self.get_cover_url(obj)

    def get_modules(self, obj):
        # Roots only: submodules arrive in the children of their parent.
        roots = sorted([m for m in obj.modules.all() if m.parent_id is None and m.is_active],
                       key=lambda m: (m.sort_order, m.id))
        return CourseModuleProgramSerializer(roots, many=True, context=self.context).data

    def get_related_products(self, obj):
        """Course products — as the same cards as in the catalog.

        We recommend a specific PACKAGING, so we return the whole product (so the card has
        price, availability, a packaging switcher and a buy button) plus
        `recommended_variant_id` — the one the card shows first.
        """
        from catalog.models import Product
        from catalog.serializers import ProductListSerializer

        want = list(obj.related_variants.filter(is_active=True, product__is_active=True)
                    .values_list('pk', 'product_id')[:12])
        if not want:
            return []
        # The order and set of prefetches are the same as in the catalog list, otherwise the card
        # pulls
        # price/availability/packagings with one query per field.
        products = {p.pk: p for p in Product.objects
                    .filter(pk__in={pid for _, pid in want}, is_active=True)
                    .select_related('paired_product', 'paired_product__brand')
                    .prefetch_related(
                        'images', 'variants__images', 'variants__variant_labels__label', 'brand',
                        'variants__attribute_values__attribute',
                        'paired_product__images',
                        'paired_product__variants__images',
                        'paired_product__variants__variant_labels__label',
                        'paired_product__variants__attribute_values__attribute',
                    )}
        out, seen = [], set()
        for vid, pid in want:
            product = products.get(pid)
            if product is None or pid in seen:
                continue
            seen.add(pid)
            row = ProductListSerializer(product, context=self.context).data
            row['recommended_variant_id'] = vid
            out.append(row)
        return out

    def get_related_bundles(self, obj):
        """Bundles — as catalog cards (BundleCard)."""
        from catalog.models import Bundle
        from catalog.serializers import BundleListSerializer

        qs = (Bundle.objects
              .filter(pk__in=obj.related_bundles.values_list('pk', flat=True), is_active=True)
              .prefetch_related(
                  'images',
                  'labels',
                  'items__product_variant__images',
                  'items__product_variant__product__images',
                  'items__product_variant__product__brand',
                  'items__product_variant__bulk_source',
              )[:12])
        return BundleListSerializer(qs, many=True, context=self.context).data


class LessonContentSerializer(serializers.ModelSerializer):
    """Full lesson content. Given only to buyers or for is_preview lessons."""
    module_id = serializers.IntegerField(read_only=True)
    module_title = serializers.CharField(source='module.title', read_only=True)
    course_slug = serializers.CharField(source='module.course.slug', read_only=True)
    course_name = serializers.CharField(source='module.course.name', read_only=True)
    video_file_url = serializers.SerializerMethodField()
    poster_url = serializers.SerializerMethodField()

    class Meta:
        model = CourseLesson
        fields = [
            'id', 'title', 'lesson_type',
            'video_file_url', 'poster_url', 'video_url', 'video_start', 'video_end',
            'content', 'duration_minutes', 'is_preview', 'sort_order',
            'module_id', 'module_title', 'course_slug', 'course_name',
        ]

    def get_video_file_url(self, obj):
        # A signed link instead of a raw MEDIA path. The view has already let in only a preview
        # lesson or a buyer, but the token is still bound to the viewer —
        # redemption checks CoursePurchase once more.
        if not obj.video_file:
            return None
        request = self.context.get('request')
        return video_access.build_url(
            request, video_access.KIND_LESSON, obj.id, _viewer_id(request),
        )

    def get_poster_url(self, obj):
        return _abs(self.context.get('request'), obj.poster.url if obj.poster else None)


class MyCourseSerializer(serializers.ModelSerializer):
    """A purchased course on the /my-courses page."""
    course = CourseListSerializer(read_only=True)

    class Meta:
        model = CoursePurchase
        fields = ['id', 'course', 'purchased_at']
