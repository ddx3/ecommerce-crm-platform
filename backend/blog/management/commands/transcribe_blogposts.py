"""
For BlogPost entries that have NO content (because YouTube has no subtitles):
1) yt-dlp downloads the audio from YouTube
2) Gemini File API transcribes the audio to text
3) The same prompt as in the regular pipeline rewrites it into an article
4) Save the content

Usage:
    python manage.py transcribe_blogposts                # all without content
    python manage.py transcribe_blogposts --limit 3      # first 3
    python manage.py transcribe_blogposts --post-id 5    # a specific post
    python manage.py transcribe_blogposts --force        # rewrite even existing content
"""

import os
import re
import tempfile

from django.conf import settings
from django.core.management.base import BaseCommand

from blog.models import BlogPost
from blog.views import _generate_article_html


YOUTUBE_RX = re.compile(r'(?:youtube\.com/(?:watch\?v=|embed/|v/)|youtu\.be/)([A-Za-z0-9_-]{6,})')


def extract_video_id(url: str) -> str:
    m = YOUTUBE_RX.search(url or '')
    return m.group(1) if m else ''


def download_audio(video_id: str, out_dir: str) -> str:
    """Downloads m4a audio and returns the path."""
    import yt_dlp
    out_template = os.path.join(out_dir, '%(id)s.%(ext)s')
    opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio',
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=True)
        return ydl.prepare_filename(info)


def transcribe_audio(audio_path: str) -> str:
    """Transcribes audio via the Gemini File API. Returns '' on failure."""
    if not getattr(settings, 'GEMINI_API_KEY', ''):
        return ''
    try:
        from google import genai
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        audio_file = client.files.upload(file=audio_path)
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                'Transcribe this audio recording in Ukrainian. Return ONLY the plain text, '
                'without timestamps, without explanations, without markdown. Add punctuation.',
                audio_file,
            ],
        )
        return (resp.text or '').strip()
    except Exception as e:
        print(f'    Gemini transcribe failed: {e}')
        return ''


class Command(BaseCommand):
    help = 'Transcribe audio for BlogPost(s) missing content'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--post-id', type=int, default=0)
        parser.add_argument('--force', action='store_true',
                            help='Process even posts that already have content')

    def handle(self, *args, **opts):
        limit = opts['limit']
        post_id = opts['post_id']
        force = opts['force']

        qs = BlogPost.objects.exclude(youtube_url='')
        if post_id:
            qs = qs.filter(pk=post_id)
        elif not force:
            qs = qs.filter(content='')
        if limit and not post_id:
            qs = qs[:limit]

        posts = list(qs)
        self.stdout.write(f'Posts to process: {len(posts)}')

        ok = failed = skipped = 0
        for i, post in enumerate(posts, 1):
            self.stdout.write(f'\n[{i}/{len(posts)}] #{post.pk} {post.title[:60]}')
            vid = extract_video_id(post.youtube_url)
            if not vid:
                self.stdout.write(self.style.WARNING('  bad youtube_url → skip'))
                skipped += 1
                continue

            with tempfile.TemporaryDirectory() as tmp:
                try:
                    self.stdout.write('  downloading audio...')
                    audio_path = download_audio(vid, tmp)
                    size_mb = os.path.getsize(audio_path) / 1024 / 1024
                    self.stdout.write(f'  audio: {size_mb:.1f} MB')

                    self.stdout.write('  transcribing via Gemini...')
                    transcript = transcribe_audio(audio_path)
                    if not transcript:
                        self.stdout.write(self.style.WARNING('  empty transcript → skip'))
                        failed += 1
                        continue
                    self.stdout.write(f'  transcript: {len(transcript)} chars')

                    self.stdout.write('  generating article...')
                    html = _generate_article_html(post.title, transcript, 'uk')
                    if not html:
                        self.stdout.write(self.style.WARNING('  empty article → skip'))
                        failed += 1
                        continue
                    self.stdout.write(f'  article: {len(html)} chars')

                    post.content = html
                    post.save(update_fields=['content', 'updated_at'])
                    self.stdout.write(self.style.SUCCESS(f'  ✓ saved'))
                    ok += 1
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'  ✗ failed: {e}'))
                    failed += 1

        self.stdout.write('\n' + '=' * 50)
        self.stdout.write(self.style.SUCCESS(f'OK:      {ok}'))
        if failed:
            self.stdout.write(self.style.ERROR(f'Failed:  {failed}'))
        if skipped:
            self.stdout.write(f'Skipped: {skipped}')
