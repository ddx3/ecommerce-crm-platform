"""Paid course videos are served only via a signed link.

The problem this closes: lesson videos live in MEDIA_ROOT and nginx served
`/media/` as plain static files. The paywall only guarded the JSON API, so anyone
could download `/media/courses/lessons/lesson-03.mp4` without an account, all the more
since the poster names (`poster-01.jpg`...) are public and hint at the video naming scheme.

The scheme now:
  1. nginx returns 404 for `/media/courses/lessons/` (see frontend/nginx.conf).
  2. Instead of a raw MEDIA URL, the API returns `/api/courses/video/<token>/`, where the token is a
     Django `signing` payload {kind, id, user}.
  3. `course_video` unpacks the token, RE-CHECKS CoursePurchase (the subscription
     may have been revoked) and serves the file via `X-Accel-Redirect` to the internal
     location `/protected-media/`, so nginx itself carries the byte stream again, with
     sendfile and Range requests, and Django does not hold 276 MB in memory.

Why a signature in the URL rather than a JWT in a header: the video is played by `<video
src>`/vidstack, and
the `<video>` element cannot send Authorization. The token is short-lived (TTL below),
so a shared link expires.
"""
from urllib.parse import quote

from django.conf import settings
from django.core import signing
from django.http import FileResponse, HttpResponse

SALT = 'courses.video.v1'

# Enough to watch a lesson in one sitting, and it limits the age of a shared
# link. The player gets a fresh token on every lesson load and re-requests one itself
# when it expires, so a shorter lifetime doesn't bother the user.
TTL_SECONDS = 2 * 60 * 60

KIND_LESSON = 'lesson'
KIND_FULL = 'full'

# Internal nginx location (`internal`), not directly reachable from outside.
INTERNAL_PREFIX = '/protected-media/'


def sign(kind: str, obj_id: int, user_id: int | None) -> str:
    return signing.dumps({'k': kind, 'i': obj_id, 'u': user_id}, salt=SALT)


def build_url(request, kind: str, obj_id: int, user_id: int | None) -> str:
    path = f'/api/courses/video/{sign(kind, obj_id, user_id)}/'
    return request.build_absolute_uri(path) if request else path


def _content_type(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith('.webm'):
        return 'video/webm'
    if lowered.endswith('.ogv'):
        return 'video/ogg'
    if lowered.endswith('.mov'):
        return 'video/quicktime'
    return 'video/mp4'


def serve(filefield, request=None) -> HttpResponse:
    """Serves the file via nginx (production) or directly (local development without nginx)."""
    ctype = _content_type(filefield.name)
    head_only = request is not None and request.method == 'HEAD'

    if not getattr(settings, 'COURSES_X_ACCEL', True):
        if head_only:
            # HEAD: the player only asks for type and size; no need to read the file.
            response = HttpResponse(status=200, content_type=ctype)
            response['Content-Length'] = str(filefield.size)
            response['Accept-Ranges'] = 'bytes'
            return response
        # There is no nginx locally, so we serve it ourselves. FileResponse streams the file
        # and handles Range, so seeking in the player works on the dev server too.
        return FileResponse(filefield.open('rb'), content_type=ctype)

    response = HttpResponse(status=200, content_type=ctype)
    # quote: file names may contain Cyrillic/spaces after upload from the admin.
    response['X-Accel-Redirect'] = INTERNAL_PREFIX + quote(filefield.name)
    # nginx sets the length and Accept-Ranges; our own empty Content-Length
    # would break the response.
    del response['Content-Length']
    return response
