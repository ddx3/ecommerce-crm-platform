import json

from django.conf import settings
from core.jsonutils import js_json
from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils import timezone

from django import forms

from catalog.widgets import QuillWidget

from core.admin_filters import IsActiveFilter
from .models import Course, CourseModule, CourseLesson, CoursePurchase


# ───── Access to the Courses section — owner only ─────
# Besides the COURSES_ENABLED feature flag, the whole section is closed to everyone except the
# owner (OWNER_EMAIL): the "Courses" item is hidden in the sidebar for the rest, and
# a direct URL (/admin/courses/…) returns 403. Same pattern as in SiteSettingsAdmin.
class _CoursesOwnerOnly:
    OWNER_EMAIL = settings.OWNER_EMAIL

    def _is_owner(self, request):
        u = getattr(request, 'user', None)
        return bool(u and u.is_authenticated and (u.email or '').lower() == self.OWNER_EMAIL)

    def has_module_permission(self, request):
        return self._is_owner(request)

    def has_view_permission(self, request, obj=None):
        return self._is_owner(request)

    def has_add_permission(self, request):
        return self._is_owner(request)

    def has_change_permission(self, request, obj=None):
        return self._is_owner(request)

    def has_delete_permission(self, request, obj=None):
        return self._is_owner(request)


# ───── Inline for editing lessons inside a module ─────

class CourseLessonInline(admin.StackedInline):
    model = CourseLesson
    extra = 0
    fields = ('sort_order', 'title', 'lesson_type', 'video_file', 'poster',
              'video_url', ('video_start', 'video_end'), 'content',
              'duration_minutes', 'is_preview')
    ordering = ('sort_order', 'id')


# ───── Course modules in the course form: reading the tree and saving edits ─────

def _modules_tree(course, request):
    """Tree of course nodes for the table in the form.

    There is one kind of node: `{mod_id, les_id, title, children, …lesson fields}`. A node always
    has a lesson — both a leaf and a section with subsections (intro video/text).
    A lesson inside a section has only les_id; a node that has subsections itself or sits
    at the root also has mod_id (a lesson in the DB does not live without a module, so the node's
    own
    lesson is stored as a lesson named after the module — CourseModule.own_lesson).
    nginx returns 404 for lesson video from /media/courses/lessons/ (paywall, see
    courses/video_access.py), so for the preview we serve a staff-only endpoint.
    """
    all_mods = list(CourseModule.objects.filter(course=course)
                    .prefetch_related('lessons').order_by('sort_order', 'id'))
    by_parent = {}
    for m in all_mods:
        by_parent.setdefault(m.parent_id, []).append(m)

    def lesson_node(l):
        poster = ''
        if l.poster:
            try:
                poster = request.build_absolute_uri(l.poster.url)
            except Exception:
                poster = ''
        return {
            'mod_id': None,
            'les_id': l.pk,
            'title': l.title,
            'is_active': bool(l.is_active),
            'lesson_type': l.lesson_type,
            'content': l.content or '',
            'duration_minutes': l.duration_minutes,
            'is_preview': bool(l.is_preview),
            'video_name': (l.video_file.name or '').split('/')[-1] if l.video_file else '',
            'video_url': ('/admin/courses/video/?kind=lesson&id=%d' % l.pk) if l.video_file else '',
            'poster_url': poster,
            'sort_order': l.sort_order,
            'children': [],
        }

    def module_node(m, seen):
        kids = [k for k in by_parent.get(m.pk, []) if k.pk not in seen]
        lessons = sorted(m.lessons.all(), key=lambda x: (x.sort_order, x.id))
        own = m.own_lesson(lessons)
        # The own lesson is the node itself, so it does not get a separate row.
        node = lesson_node(own) if own else {
            'mod_id': None, 'les_id': None, 'title': m.title, 'is_active': True,
            'lesson_type': CourseLesson.LESSON_VIDEO, 'content': '',
            'duration_minutes': None, 'is_preview': False,
            'video_name': '', 'video_url': '', 'poster_url': '', 'children': [],
        }
        node['mod_id'] = m.pk
        node['title'] = m.title
        node['is_active'] = bool(m.is_active)
        node['sort_order'] = m.sort_order
        children = [lesson_node(l) for l in lessons if l is not own]
        for k in kids:
            seen.add(k.pk)
            children.append(module_node(k, seen))
        # Lessons and subsections share one sort_order within the parent; on a tie
        # (old structure with separate counters) lessons come first.
        children.sort(key=lambda n: (n.get('sort_order') or 0, 0 if n.get('les_id') else 1))
        node['children'] = children
        return node

    seen = set()
    out = []
    for m in by_parent.get(None, []):
        if m.pk in seen:          # guard against a cycle in the data
            continue
        seen.add(m.pk)
        out.append(module_node(m, seen))
    return out


def _apply_modules_tree(course, tree, files):
    """Brings the course structure to the state submitted by the form.

    `tree` — tree of nodes: [{mod_id|None, les_id|None, key, title, children:[…]}].
    A node with children (or a root node) is saved as a module; if it has lesson
    content, also as a lesson of the same name inside it (the node's own lesson).
    A node without children inside a section is just a lesson.
    The order in the list is the sort_order — shared by lessons and subsections within the
    parent, so they can interleave.
    Files come as separate inputs lesson_video__<key> / lesson_poster__<key> —
    the JSON carries only the "clear" flags.
    Deletions are computed ONCE at the end over the whole course: a lesson or subsection may have
    moved to another parent, and per-block cleanup would wipe them as 'extras'.
    """
    keep_modules, keep_lessons = [], []

    def node_title(n):
        # 200 is the model field limit; anything longer would crash the whole submit with a 500 and
        # the user would lose the whole form, so we just truncate.
        return ((n.get('title') or '').strip() or 'Untitled')[:200]

    def save_module(n, parent, order):
        mod = None
        if n.get('mod_id'):
            mod = CourseModule.objects.filter(pk=n['mod_id'], course=course).first()
        if mod is None:
            mod = CourseModule(course=course)
        mod.parent = parent
        mod.title = node_title(n)
        mod.is_active = bool(n.get('is_active', True))
        mod.sort_order = order
        mod.save()
        keep_modules.append(mod.pk)
        return mod

    def save_lesson(n, module, order):
        les = None
        if n.get('les_id'):
            les = CourseLesson.objects.filter(pk=n['les_id'], module__course=course).first()
        if les is None:
            les = CourseLesson(module=module)
        les.module = module
        les.title = node_title(n)
        les.is_active = bool(n.get('is_active', True))
        les.sort_order = order
        les.content = n.get('content') or ''
        try:
            dur = int(n.get('duration_minutes'))
            les.duration_minutes = dur if dur > 0 else None
        except (TypeError, ValueError):
            les.duration_minutes = None
        les.is_preview = bool(n.get('is_preview'))

        key = str(n.get('key') or '')
        vf = files.get('lesson_video__%s' % key) if key else None
        if vf is not None:
            les.video_file = vf
        elif n.get('video_clear'):
            les.video_file = None
        pf = files.get('lesson_poster__%s' % key) if key else None
        if pf is not None:
            les.poster = pf
        elif n.get('poster_clear'):
            les.poster = None
        # We do not ask for the type in the form — it follows from the content: if there is a video
        # (file or
        # external link) → video, otherwise text.
        les.lesson_type = (CourseLesson.LESSON_VIDEO
                           if (les.video_file or les.video_url)
                           else CourseLesson.LESSON_TEXT)
        les.save()
        keep_lessons.append(les.pk)
        return les

    def has_lesson(n):
        """Whether the node has its own lesson — to avoid creating empty lessons in sections."""
        if n.get('les_id'):
            return True
        if (n.get('content') or '').strip():
            return True
        if n.get('duration_minutes') or n.get('is_preview'):
            return True
        key = str(n.get('key') or '')
        return bool(key and (files.get('lesson_video__%s' % key)
                             or files.get('lesson_poster__%s' % key)))

    def walk(nodes, parent):
        for i, n in enumerate(nodes):
            if not isinstance(n, dict):
                continue
            kids = [k for k in (n.get('children') or []) if isinstance(k, dict)]
            if kids or parent is None:
                # A section and any root node is a module; the node's own lesson lives
                # inside it as a lesson of the same name. sort_order 0: it does not take part
                # in the shared numbering of children — it is fetched separately
                # (CourseModule.own_lesson),
                # so a collision affects nothing.
                mod = save_module(n, parent, i)
                if kids:
                    if has_lesson(n):
                        save_lesson(n, mod, 0)
                    walk(kids, mod)
                else:
                    save_lesson(n, mod, 0)
            else:
                save_lesson(n, parent, i)

    walk(tree, None)

    CourseLesson.objects.filter(module__course=course).exclude(pk__in=keep_lessons).delete()
    CourseModule.objects.filter(course=course).exclude(pk__in=keep_modules).delete()


# ───── Course modules ─────

class CourseModuleAdmin(_CoursesOwnerOnly, admin.ModelAdmin):
    """
    Modules are edited inside the course form (inline + the "Lessons" button).
    This admin stays registered for the sake of the change-form URL (the "Lessons (N)"
    button in the course form leads there), but is hidden from the /admin/ menu via
    get_model_perms — so the Courses section has ONE item, "Courses".
    """
    list_display = ('id', 'course_link', 'sort_order', 'title_col', 'lessons_badge')
    list_display_links = None  # row click goes through title_col
    list_editable = ('sort_order',)
    list_filter = ('course',)
    search_fields = ('title', 'course__name')
    ordering = ('course', 'sort_order')
    inlines = [CourseLessonInline]
    list_per_page = 50
    # Without save_on_top Django does not render {% block submit_buttons_top %}, and
    # the page kept the standard bottom submit-row instead of the CRM header.
    save_on_top = True
    change_list_template = 'admin/courses/coursemodule/change_list.html'

    def changelist_view(self, request, extra_context=None):
        import json
        items = []
        for m in CourseModule.objects.select_related('course').prefetch_related('lessons').all():
            items.append({
                'id': m.id, 'title': m.title, 'sort_order': m.sort_order,
                'course_id': m.course_id, 'course_name': m.course.name if m.course else '',
                'lessons_count': m.lessons.count(),
            })
        courses = [{'id': c.id, 'name': c.name} for c in Course.objects.all()]
        ctx = extra_context or {}
        ctx['modules_json'] = js_json(items, ensure_ascii=False)
        ctx['courses_json'] = js_json(courses, ensure_ascii=False)
        return super().changelist_view(request, extra_context=ctx)

    def get_model_perms(self, request):
        # Empty perms hide the model from the admin index / sidebar, but the
        # change/add URLs keep working (for FKs and direct navigation).
        return {}

    @admin.display(description='Course', ordering='course__name')
    def course_link(self, obj):
        return format_html(
            '<a href="/admin/courses/course/{}/change/" '
            'style="color:#374151;text-decoration:none;font-weight:600;">{}</a>',
            obj.course_id, obj.course.name,
        )

    @admin.display(description='Module title')
    def title_col(self, obj):
        return format_html(
            '<a href="/admin/courses/coursemodule/{}/change/" '
            'style="color:#1f2937;text-decoration:none;font-weight:500;">{}</a>',
            obj.pk, obj.title,
        )

    @admin.display(description='Lessons')
    def lessons_badge(self, obj):
        n = obj.lessons.count()
        color = '#374151' if n > 0 else '#9ca3af'
        return format_html(
            '<span style="display:inline-block;min-width:30px;text-align:center;'
            'background:#f3f4f6;border:1px solid #e5e7eb;color:{};padding:2px 8px;'
            'border-radius:3px;font-size:11px;font-weight:600;">{}</span>',
            color, n,
        )


# ───── Inline for modules inside a course ─────

class CourseModuleInline(admin.TabularInline):
    model = CourseModule
    extra = 0
    fields = ('sort_order', 'title', 'edit_link')
    readonly_fields = ('edit_link',)
    ordering = ('sort_order', 'id')
    verbose_name = 'Module'
    verbose_name_plural = 'Course modules'

    @admin.display(description='Lessons')
    def edit_link(self, obj):
        if not obj.pk:
            return '—'
        n = obj.lessons.count()
        label = f'Lessons ({n})' if n else 'Add lessons'
        return format_html(
            '<a href="/admin/courses/coursemodule/{}/change/">{}</a>',
            obj.pk, label,
        )


# ───── Inline for course purchases (view only) ─────

class CoursePurchaseInline(admin.TabularInline):
    model = CoursePurchase
    extra = 0
    fields = ('user_display', 'purchased_at_display', 'order_link')
    readonly_fields = ('user_display', 'purchased_at_display', 'order_link')
    can_delete = False
    show_change_link = False
    verbose_name = 'Purchase'
    verbose_name_plural = 'Purchases of this course'
    ordering = ('-purchased_at',)

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='User')
    def user_display(self, obj):
        u = obj.user
        full = ' '.join(filter(None, [u.first_name, u.last_name])) or u.email
        return f'{full} · {u.email}'

    @admin.display(description='Date')
    def purchased_at_display(self, obj):
        return timezone.localtime(obj.purchased_at).strftime('%d.%m.%Y %H:%M')

    @admin.display(description='Order')
    def order_link(self, obj):
        if not obj.order_item_id or not obj.order_item:
            return '— manual —'
        order = obj.order_item.order
        return format_html(
            '<a href="/admin/orders/order/{}/change/">#{}</a>',
            order.pk, order.order_number,
        )


class SelectedOnlySelectMultiple(forms.SelectMultiple):
    """<select multiple> that renders ONLY the selected options.

    The full list is not needed in the DOM: items are added by the "Products and bundles"
    table (which also appends an <option> for the one just chosen).
    Otherwise the page pulled ~470 product options and ~60 bundles that nobody sees.
    We do not touch validation — the field's own queryset stays full, so Django
    still checks that the submitted ids exist.
    """

    def optgroups(self, name, value, attrs=None):
        selected = {str(v) for v in (value or []) if v not in ('', None)}
        if not selected:
            return []
        out = []
        for group, options, index in super().optgroups(name, value, attrs):
            keep = [o for o in options if str(o['value']) in selected]
            if keep:
                out.append((group, keep, index))
        return out


# ───── Courses ─────

class CourseAdmin(_CoursesOwnerOnly, admin.ModelAdmin):
    # We do not show the slug field in the form — Course.save() generates it from the title
    # (unique_slug) and does not touch an existing one. So prepopulated_fields is not needed either.
    list_display = (
        'cover_thumb', 'name_col', 'price_col',
        'modules_badge', 'lessons_badge', 'purchases_badge',
        'is_active_pill', 'sort_order',
    )
    list_display_links = None
    # WITHOUT list_editable: visibility and price are saved instantly via fetch
    # save-course-field (as with products). list_editable rendered a hidden formset,
    # and the "Save" button overwrote them with the page state at load time.
    list_filter = (IsActiveFilter, 'related_categories')
    search_fields = ('name', 'slug', 'instructor_name')
    ordering = ('sort_order', '-created_at')
    # WITHOUT filter_horizontal: Django's two-pane widget took ~580px, and products
    # do not have one — they use a compact .picker-trigger. We keep the plain
    # <select multiple>, and the template builds the picker on top of it.
    inlines = [CourseModuleInline, CoursePurchaseInline]
    list_per_page = 50
    save_on_top = True
    change_list_template = 'admin/courses/course/change_list.html'

    def changelist_view(self, request, extra_context=None):
        import json
        items = []
        # List columns: photo / title / price / visibility. We do not show counters of modules,
        # lessons and purchases in the table — so we do not compute them either.
        qs = Course.objects.prefetch_related('related_categories').all()
        for c in qs:
            cover = ''
            if c.cover_image:
                try:
                    cover = request.build_absolute_uri(c.cover_image.url)
                except Exception:
                    pass
            items.append({
                'id': c.id, 'name': c.name,
                'cover': cover,
                'price': float(c.price),
                'is_active': c.is_active,
                'category_ids': [cat.id for cat in c.related_categories.all()],
            })
        from catalog.models import Category
        cats = [{'id': c.id, 'name': c.name} for c in Category.objects.all()]
        ctx = extra_context or {}
        ctx['courses_json'] = js_json(items, ensure_ascii=False)
        ctx['categories_json'] = js_json(cats, ensure_ascii=False)
        return super().changelist_view(request, extra_context=ctx)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Quill instead of Summernote: the single CRM editor, styles are global in
        # base_site.html (.quill-wrapper). Summernote rendered an iframe and pulled
        # jQuery+Bootstrap from external CDNs straight into the admin.
        if 'description' in form.base_fields:
            form.base_fields['description'].widget = QuillWidget(label='Course description')
        if 'short_description' in form.base_fields:
            form.base_fields['short_description'].widget.attrs['rows'] = 3
        for f in ('related_variants', 'related_bundles'):
            if f in form.base_fields:
                form.base_fields[f].widget = SelectedOnlySelectMultiple()
        # We do not show hints under fields (the product form has none either):
        # related_* — stock Django text about Control/Command,
        # short_description — "1-2 sentences for the card…".
        # full_video_chapters: the JSON format hint is no longer needed —
        # the form has a rows editor "min:sec + title", raw JSON is not visible.
        for f in ('related_variants', 'related_categories', 'related_bundles',
                  'short_description', 'full_video_file', 'full_video_chapters'):
            if f in form.base_fields:
                form.base_fields[f].help_text = ''
        return form

    def get_inlines(self, request, obj=None):
        # There are no inlines in the course form at all:
        #   modules — own list in change_form (drag and drop, collapsing, lessons
        #            with video/photo/text blocks), saved by _apply_modules_tree;
        #   purchases — temporarily removed from the form (CoursePurchaseInline stays
        #            defined so it can be restored in one line).
        return []

    def render_change_form(self, request, context, *args, **kwargs):
        obj = kwargs.get('obj') or context.get('original')
        context['modules_json'] = js_json(_modules_tree(obj, request) if obj else [])
        # The lesson editor uses the same toolbar as the course/product description (QuillWidget),
        # so the set of buttons does not drift apart.
        from catalog.widgets import QUILL_TOOLBAR
        context['quill_toolbar_json'] = js_json(QUILL_TOOLBAR)
        return super().render_change_form(request, context, *args, **kwargs)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # There is NO empty string when JS works: even 'no modules' is "[]".
        # So we treat blank as 'the block was not rendered' and do not touch modules at all —
        # otherwise a JS failure would silently wipe all lessons of the course.
        raw = request.POST.get('course_modules_json') or ''
        if not raw.strip():
            return
        try:
            tree = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(tree, list):
            _apply_modules_tree(form.instance, tree, request.FILES)
    fieldsets = (
        # Removed from the form (fields stay in the model, values are untouched):
        #   slug           — generated from the title in Course.save()
        #   sort_order     — order on the site = publication date (-created_at)
        #   instructor_name — courses are always from the shop, one author
        #   old_price      — we do not keep an old price
        ('Main', {
            'fields': ('name', 'is_active', 'cover_image', 'price'),
        }),
        ('Description', {
            # There is no short description in the form: a course has ONE description, and the
            # catalog card
            # teaser is cut from it by the serializer (_teaser).
            'fields': ('description',),
        }),
        ('Full video', {
            # There is no separate poster: a course has ONE photo (the cover) — it is on the catalog
            # card,
            # on the course page, and serves as the poster of the full video.
            'fields': ('full_video_file', 'full_video_chapters'),
        }),
        ('Shop', {
            # Categories — a compact picker, products and bundles — a table
            # (the template merges the two fields into one table).
            # related_bundles stays in the form even though its row is hidden —
            # its <select> is what submits the bundles from the table in the POST.
            'fields': ('related_variants', 'related_bundles'),
        }),
    )

    @admin.display(description='')
    def cover_thumb(self, obj):
        if not obj.cover_image:
            return mark_safe(
                '<div style="width:60px;height:40px;background:#f3f4f6;'
                'border:1px solid #e5e7eb;border-radius:4px;display:flex;'
                'align-items:center;justify-content:center;color:#9ca3af;font-size:18px;">📚</div>'
            )
        return format_html(
            '<img src="{}" style="width:60px;height:40px;object-fit:cover;'
            'border-radius:4px;border:1px solid #e5e7eb;" />',
            obj.cover_image.url,
        )

    @admin.display(description='Course', ordering='name')
    def name_col(self, obj):
        sub = obj.instructor_name or '—'
        return format_html(
            '<div style="line-height:1.3;">'
            '<a href="/admin/courses/course/{}/change/" '
            'style="color:#1f2937;text-decoration:none;font-weight:600;">{}</a>'
            '<div style="font-size:11px;color:#9ca3af;margin-top:2px;">{}</div>'
            '</div>',
            obj.pk, obj.name, sub,
        )

    @admin.display(description='Price', ordering='price')
    def price_col(self, obj):
        if obj.old_price and obj.old_price > obj.price:
            return format_html(
                '<div style="line-height:1.2;">'
                '<span style="font-weight:700;color:#1f2937;">{} ₴</span><br>'
                '<span style="text-decoration:line-through;color:#9ca3af;font-size:11px;">{} ₴</span>'
                '</div>',
                obj.price, obj.old_price,
            )
        return format_html('<span style="font-weight:700;color:#1f2937;">{} ₴</span>', obj.price)

    @admin.display(description='Modules')
    def modules_badge(self, obj):
        n = obj.modules.count()
        return self._badge(n, n > 0)

    @admin.display(description='Lessons')
    def lessons_badge(self, obj):
        n = obj.total_lessons
        return self._badge(n, n > 0)

    @admin.display(description='Purchased')
    def purchases_badge(self, obj):
        n = obj.purchases.count()
        if n == 0:
            return mark_safe('<span style="color:#9ca3af;font-size:11px;">—</span>')
        return format_html(
            '<span style="display:inline-block;min-width:30px;text-align:center;'
            'background:#fef3c7;border:1px solid #fde68a;color:#92400e;padding:2px 8px;'
            'border-radius:3px;font-size:11px;font-weight:600;">{}</span>', n,
        )

    @admin.display(description='Active', ordering='is_active')
    def is_active_pill(self, obj):
        if obj.is_active:
            return mark_safe(
                '<span style="display:inline-block;background:#dcfce7;border:1px solid #bbf7d0;'
                'color:#166534;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600;">Active</span>'
            )
        return mark_safe(
            '<span style="display:inline-block;background:#f3f4f6;border:1px solid #e5e7eb;'
            'color:#6b7280;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600;">Hidden</span>'
        )

    @staticmethod
    def _badge(n, accent):
        color = '#374151' if accent else '#9ca3af'
        return format_html(
            '<span style="display:inline-block;min-width:30px;text-align:center;'
            'background:#f3f4f6;border:1px solid #e5e7eb;color:{};padding:2px 8px;'
            'border-radius:3px;font-size:11px;font-weight:600;">{}</span>',
            color, n,
        )


# ───── Course purchases ─────

class CoursePurchaseAdmin(_CoursesOwnerOnly, admin.ModelAdmin):
    list_display = ('id', 'user_col', 'course_col', 'purchased_at_col', 'order_link')
    list_filter = ('course',)
    search_fields = ('user__email', 'user__first_name', 'user__last_name', 'course__name')
    ordering = ('-purchased_at',)
    autocomplete_fields = ('user', 'course')
    # Order item is raw_id: a regular <select> rendered all ~55,000 OrderItem (one query per
    # option), the page
    # took ~1 min to open and weighed 5 MB.
    raw_id_fields = ('order_item',)
    readonly_fields = ('purchased_at',)
    list_per_page = 50
    date_hierarchy = 'purchased_at'
    change_list_template = 'admin/courses/coursepurchase/change_list.html'

    def changelist_view(self, request, extra_context=None):
        import json
        items = []
        qs = (CoursePurchase.objects.select_related('user', 'course', 'order_item', 'order_item__order')
              .order_by('-purchased_at'))
        for p in qs:
            u = p.user
            full = ' '.join(filter(None, [u.first_name, u.last_name])) or u.email
            order_num = ''
            order_id = ''
            if p.order_item_id and p.order_item and p.order_item.order:
                order_num = p.order_item.order.order_number
                order_id = p.order_item.order_id
            items.append({
                'id': p.id,
                'user_full': full,
                'user_email': u.email,
                'course_id': p.course_id,
                'course_name': p.course.name if p.course else '',
                'purchased_at': timezone.localtime(p.purchased_at).strftime('%d.%m.%Y %H:%M'),
                'purchased_at_iso': timezone.localtime(p.purchased_at).strftime('%Y-%m-%d'),
                'order_id': order_id,
                'order_number': order_num,
            })
        courses = [{'id': c.id, 'name': c.name} for c in Course.objects.all()]
        ctx = extra_context or {}
        ctx['purchases_json'] = js_json(items, ensure_ascii=False)
        ctx['courses_json'] = js_json(courses, ensure_ascii=False)
        return super().changelist_view(request, extra_context=ctx)

    @admin.display(description='User', ordering='user__email')
    def user_col(self, obj):
        u = obj.user
        full = ' '.join(filter(None, [u.first_name, u.last_name])) or u.email
        return format_html(
            '<div style="line-height:1.3;">'
            '<div style="font-weight:600;color:#1f2937;">{}</div>'
            '<div style="font-size:11px;color:#9ca3af;">{}</div>'
            '</div>', full, u.email,
        )

    @admin.display(description='Course', ordering='course__name')
    def course_col(self, obj):
        return format_html(
            '<a href="/admin/courses/course/{}/change/" '
            'style="color:#374151;text-decoration:none;font-weight:500;">{}</a>',
            obj.course_id, obj.course.name,
        )

    @admin.display(description='Date', ordering='purchased_at')
    def purchased_at_col(self, obj):
        return timezone.localtime(obj.purchased_at).strftime('%d.%m.%Y %H:%M')

    @admin.display(description='Order')
    def order_link(self, obj):
        if not obj.order_item_id or not obj.order_item:
            return mark_safe('<span style="color:#9ca3af;">— manual —</span>')
        order = obj.order_item.order
        paid = order.payment_status == 'paid'
        color = '#166534' if paid else '#92400e'
        bg = '#dcfce7' if paid else '#fef3c7'
        border = '#bbf7d0' if paid else '#fde68a'
        return format_html(
            '<a href="/admin/orders/order/{}/change/" '
            'style="display:inline-block;background:{};border:1px solid {};color:{};'
            'padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600;text-decoration:none;">'
            '#{}</a>', order.pk, bg, border, color, order.order_number,
        )

    def has_add_permission(self, request):
        return False

    def get_model_perms(self, request):
        # Hide "Course purchases" from the menu/index — ONE item, "Courses",
        # remains. The URL for viewing a specific purchase keeps working (via FK).
        return {}


# ───── Registration under a feature flag ─────
# COURSES_ENABLED=False in backend/.env hides the whole Courses section in /admin/.
# Models, API and signals keep working — only the admin UI is turned off.
if settings.COURSES_ENABLED:
    admin.site.register(CourseModule, CourseModuleAdmin)
    admin.site.register(Course, CourseAdmin)
    admin.site.register(CoursePurchase, CoursePurchaseAdmin)
