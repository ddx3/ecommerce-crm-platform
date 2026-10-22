from django.conf import settings
from django.db import models
from slugify import slugify

from catalog.models import Category, unique_slug, _site_url


class Course(models.Model):
    name = models.CharField(max_length=200, verbose_name='Name')
    slug = models.SlugField(max_length=200, unique=True, blank=True, verbose_name='Slug')
    short_description = models.TextField(
        blank=True, verbose_name='Short description',
        help_text='1–2 sentences for the card in the course catalog.',
    )
    description = models.TextField(blank=True, verbose_name='Description (HTML)')
    cover_image = models.ImageField(upload_to='courses/', blank=True, null=True, verbose_name='Cover image')

    full_video_file = models.FileField(
        upload_to='courses/lessons/', blank=True, null=True,
        verbose_name='Full course video',
        help_text='Optional. The full version as a single recording, shown '
                  'as a separate block above the modules. Available only to those who bought the course.',
    )
    full_video_poster = models.ImageField(
        upload_to='courses/posters/', blank=True, null=True,
        verbose_name='Full video poster',
    )
    full_video_chapters = models.JSONField(
        default=list, blank=True,
        verbose_name='Full video chapters',
        help_text='Timecodes for navigating the full video. Format: '
                  '[{"time": 93, "title": "Chapter title"}], where time is in seconds '
                  'from the start. Shown as buttons under the player.',
    )

    instructor_name = models.CharField(max_length=200, blank=True, verbose_name='Author/instructor')

    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Price')
    old_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name='Old price',
    )

    is_active = models.BooleanField(default=True, db_index=True, verbose_name='Shown')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='Sort order')

    # Granularity is the PACKAGING, not the product: price, stock level and 'own packaging' live
    # on ProductVariant, so recommending 'a product in general' does not work — it is unclear
    # which packaging to show and at what price.
    related_variants = models.ManyToManyField(
        'catalog.ProductVariant', blank=True, related_name='related_courses',
        verbose_name='Related products',
    )
    related_categories = models.ManyToManyField(
        Category, blank=True, related_name='related_courses',
        verbose_name='Related categories',
    )
    related_bundles = models.ManyToManyField(
        'catalog.Bundle', blank=True, related_name='related_courses',
        verbose_name='Related bundles',
    )

    meta_title = models.CharField(max_length=200, blank=True, verbose_name='Meta Title')
    meta_description = models.TextField(blank=True, verbose_name='Meta Description')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Course'
        verbose_name_plural = 'Courses'
        ordering = ['sort_order', '-created_at']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self.__class__, slugify(self.name), self.pk)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        # Provides the "Go to site" button in the form header
        # (_submit_buttons_top.html).
        return _site_url(f"/course/{self.slug}")

    @property
    def total_lessons(self) -> int:
        # Visible only: hidden lessons and sections do not reach the storefront,
        # so they cannot be counted in 'N lessons' — the number would not match the list.
        return CourseLesson.objects.filter(
            module__course=self, is_active=True, module__is_active=True).count()

    @property
    def total_duration_minutes(self) -> int:
        agg = CourseLesson.objects.filter(
            module__course=self, is_active=True, module__is_active=True
        ).aggregate(total=models.Sum('duration_minutes'))
        return agg['total'] or 0


class CourseModule(models.Model):
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name='modules',
        verbose_name='Course',
    )
    # Nesting: a module can be a submodule of another (levels 1 → 1.1 → 1.1.1).
    # sort_order is unique within its parent, not the whole course.
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True,
        related_name='children', verbose_name='Parent module',
    )
    title = models.CharField(max_length=200, verbose_name='Module title')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='Order')
    # A hidden module is not shown in the storefront curriculum (together with its lessons).
    is_active = models.BooleanField(default=True, verbose_name='Shown')

    class Meta:
        verbose_name = 'Course module'
        verbose_name_plural = 'Course modules'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.title

    def own_lesson(self, lessons=None):
        """A node's own lesson is the one with the same name as the module.

        Module and lesson are one entity: a node has one name, and it can itself be a
        lesson (video, poster, text) even if it has subsections inside. A lesson
        in the DB does not live without a module, so the node's own lesson is stored as a regular
        CourseLesson named after the module — and in the CRM and on the storefront it is shown by
        the node itself, not as a separate row.
        """
        lessons = list(self.lessons.all() if lessons is None else lessons)
        title = (self.title or '').strip()
        own = [l for l in lessons if (l.title or '').strip() == title]
        own.sort(key=lambda l: (l.sort_order, l.id))
        return own[0] if own else None

    def is_leaf_wrapper(self, lessons=None, children=None):
        """A node without subsections and without other lessons is just a lesson on one row."""
        lessons = list(self.lessons.all() if lessons is None else lessons)
        children = list(self.children.all() if children is None else children)
        own = self.own_lesson(lessons)
        return bool(own) and not children and len(lessons) == 1


class CourseLesson(models.Model):
    LESSON_VIDEO = 'video'
    LESSON_TEXT = 'text'
    LESSON_TYPE_CHOICES = [
        (LESSON_VIDEO, 'Video (embed)'),
        (LESSON_TEXT, 'Text'),
    ]

    module = models.ForeignKey(
        CourseModule, on_delete=models.CASCADE, related_name='lessons',
        verbose_name='Module',
    )
    title = models.CharField(max_length=200, verbose_name='Lesson title')
    sort_order = models.PositiveIntegerField(default=0, verbose_name='Order')
    is_active = models.BooleanField(default=True, verbose_name='Shown')

    lesson_type = models.CharField(
        max_length=10, choices=LESSON_TYPE_CHOICES, default=LESSON_VIDEO,
        verbose_name='Lesson type',
    )
    video_file = models.FileField(
        upload_to='courses/lessons/', blank=True, null=True,
        verbose_name='Video file',
        help_text='Upload the lesson video. If filled in, it plays in the built-in '
                  'player. Takes priority over "Video URL".',
    )
    poster = models.ImageField(
        upload_to='courses/posters/', blank=True, null=True,
        verbose_name='Photo',
        help_text='Splash image for the player and the lesson list.',
    )
    video_url = models.URLField(
        blank=True, verbose_name='Video URL (external)',
        help_text='Fallback: a link to YouTube/Vimeo if no video file '
                  'is uploaded. The embed is built automatically.',
    )
    video_start = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='Start, sec',
        help_text='Start timecode of the external video fragment. Leave empty to start from the beginning.',
    )
    video_end = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='End, sec',
        help_text='End timecode of the external video fragment. Leave empty to play to the end.',
    )
    content = models.TextField(blank=True, verbose_name='Lesson text (HTML)')

    duration_minutes = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='Duration, min',
    )
    is_preview = models.BooleanField(
        default=False, verbose_name='Free preview',
        help_text='The lesson is available without buying the course.',
    )

    class Meta:
        verbose_name = 'Lesson'
        verbose_name_plural = 'Lessons'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.title


class CoursePurchase(models.Model):
    """
    Created by the signal `_course_grant_access_on_paid` (courses/signals.py)
    on the Order.payment_status transition pending → paid, for all OrderItems
    of that order with course_id set.

    Uniqueness of (user, course) protects against duplicates on repeated payment webhooks.
    Guest orders (order.user is None) are skipped — there is nobody to grant access to.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='course_purchases', verbose_name='User',
    )
    # PROTECT: a course with purchases cannot be deleted 'by brute force' — otherwise real
    # buyers would instantly lose access. Courses are not deleted anyway, they are hidden
    # (is_active=False). The financial fact of payment is stored separately in Order/OrderItem.
    course = models.ForeignKey(
        Course, on_delete=models.PROTECT,
        related_name='purchases', verbose_name='Course',
    )
    order_item = models.ForeignKey(
        'orders.OrderItem', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
        verbose_name='Order item',
    )
    purchased_at = models.DateTimeField(auto_now_add=True, verbose_name='Purchase date')

    class Meta:
        verbose_name = 'Course purchase'
        verbose_name_plural = 'Course purchases'
        ordering = ['-purchased_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'course'], name='coursepurchase_uniq_user_course'),
        ]

    def __str__(self):
        return f'{self.user} → {self.course}'
