from django.conf import settings
from django.db import models
from slugify import slugify

from catalog.models import unique_slug


class BlogPost(models.Model):
    title = models.CharField('Title', max_length=200)
    slug = models.SlugField('Slug', max_length=220, unique=True, blank=True)
    is_active = models.BooleanField('Shown', default=True)
    preview_image = models.ImageField('Preview', upload_to='blog/', blank=True, null=True)
    content = models.TextField('Content', blank=True)
    youtube_url = models.URLField('YouTube link', max_length=500, blank=True)
    meta_title = models.CharField('SEO Title', max_length=200, blank=True,
                                  help_text='Title in Google search results. Leave empty to use the article title.')
    meta_description = models.TextField('SEO Description', blank=True,
                                        help_text='Description in Google search results (up to ~160 characters). Leave empty to use the beginning of the article.')
    created_at = models.DateTimeField('Created', auto_now_add=True)
    updated_at = models.DateTimeField('Updated', auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Article'
        verbose_name_plural = 'Blog'

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        base = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
        return f"{base}/blog/{self.slug}"

    def save(self, *args, **kwargs):
        base = slugify(self.title) or 'post'
        self.slug = unique_slug(BlogPost, base, self.pk)
        super().save(*args, **kwargs)
