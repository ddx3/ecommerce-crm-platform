import base64
import json
import logging
import re
import urllib.parse
import urllib.request

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse, HttpResponseBadRequest


logger = logging.getLogger(__name__)

YOUTUBE_RX = re.compile(r'(?:youtube\.com/(?:watch\?v=|embed/|v/)|youtu\.be/)([A-Za-z0-9_-]{6,})')

# Preferred subtitle languages — uk has priority, then ru/en as a fallback (the model will translate
# to uk anyway)
TRANSCRIPT_LANGS = ['uk', 'ru', 'en']


def _http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    return urllib.request.urlopen(req, timeout=timeout)


def _basic_title_cleanup(s: str) -> str:
    s = (s or '').strip()
    s = s.replace(' ', ' ').replace('​', '')
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def _clean_title_with_ai(raw: str) -> str:
    """Removes extra spaces/special characters, fixes obvious errors in a YouTube video title."""
    cleaned = _basic_title_cleanup(raw)
    if not cleaned:
        return cleaned
    try:
        from core import llm
        prompt = (
            'You are given the original title of a YouTube video. '
            'It will be used as an article title in the Ukrainian blog of a shop selling fishing/hunting/camping goods.\n'
            'Make it tidy: remove extra spaces, repetitions, quotes/emoji/CAPS LOCK, fix obvious typos. '
            'Keep the language and the meaning of the original. Do not add anything of your own. No period at the end.\n'
            'Return JSON of the form {"title": "..."}.'
        )
        data = llm.text_to_json(f'{prompt}\n\nOriginal:\n{cleaned}')
        improved = _basic_title_cleanup(str(data.get('title') or ''))
        return improved or cleaned
    except Exception:
        return cleaned


@staff_member_required
def fetch_youtube_info(request):
    """Returns the title and preview image of a YouTube video for auto-filling the form."""
    url = (request.GET.get('url') or '').strip()
    if not url:
        return HttpResponseBadRequest('url required')
    match = YOUTUBE_RX.search(url)
    if not match:
        return JsonResponse({'error': 'Invalid YouTube URL'}, status=400)
    video_id = match.group(1)

    # 1) Title — via oEmbed (no API key)
    title = ''
    try:
        oembed_url = 'https://www.youtube.com/oembed?' + urllib.parse.urlencode({
            'url': f'https://www.youtube.com/watch?v={video_id}',
            'format': 'json',
        })
        with _http_get(oembed_url) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        title = data.get('title', '') or ''
    except Exception as e:
        return JsonResponse({'error': f'YouTube oEmbed failed: {e}'}, status=502)

    title = _clean_title_with_ai(title)

    # 2) Preview — try in order from best quality
    thumbnail_data = None
    for variant in ('maxresdefault', 'sddefault', 'hqdefault', 'mqdefault'):
        try:
            with _http_get(f'https://img.youtube.com/vi/{video_id}/{variant}.jpg') as resp:
                if resp.status == 200:
                    thumbnail_data = resp.read()
                    if thumbnail_data:
                        break
        except Exception:
            continue

    if not thumbnail_data:
        return JsonResponse({'error': 'Could not get the preview'}, status=502)

    b64 = base64.b64encode(thumbnail_data).decode('ascii')

    # 3) Transcript + AI article. If there are no subtitles — return without content_html,
    #    the frontend will just save the post with only the video + title + preview.
    transcript_text, transcript_lang = _fetch_transcript(video_id)
    content_html = ''
    if transcript_text:
        content_html = _generate_article_html(title, transcript_text, transcript_lang) or ''

    return JsonResponse({
        'title': title,
        'video_id': video_id,
        'thumbnail_data_url': f'data:image/jpeg;base64,{b64}',
        'thumbnail_filename': f'youtube-{video_id}.jpg',
        'content_html': content_html,
        'transcript_lang': transcript_lang or '',
    })


def _fetch_transcript(video_id: str) -> tuple[str, str]:
    """Returns (text, language_code) or ('', '') if there are no subtitles."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        logger.warning('youtube-transcript-api not installed')
        return '', ''
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=TRANSCRIPT_LANGS)
        segments = fetched.to_raw_data()
        text = ' '.join((s.get('text') or '').strip() for s in segments).strip()
        text = re.sub(r'\s+', ' ', text)
        return text, fetched.language_code or ''
    except Exception as e:
        logger.info('No transcript for %s: %s', video_id, e)
        return '', ''


def _generate_article_html(title: str, transcript: str, src_lang: str) -> str:
    """Asks Gemini to rewrite the transcript into a blog article in Ukrainian. Returns HTML or ''."""
    if not transcript:
        return ''
    try:
        from core import llm
        # Limit the input — in very long videos the transcript can be > 50k characters,
        # for a blog article 30k is more than enough.
        clipped = transcript[:30000]

        prompt = (
            'You are the editor of the blog of the online garden-goods shop "Magiya Roslyn". '
            'You are given a "raw" transcript of a YouTube video (often auto-recognized, without punctuation). '
            'Your task is to extract ONLY THE KEY POINTS on the topic and write them up as LIVELY TEXT, '
            'not as a technical "cheat sheet". The buyer reads it as a short column, not as an instruction.\n\n'
            'What we throw out: greetings, channel ads, "like/subscribe", '
            'personal stories with no use, repetitions, digressions, promises to tell more later.\n\n'
            'What we keep: concrete advice, doses, timings, names of products/plants, warnings, '
            'hard numbers, cause-and-effect links ("if... then...").\n\n'
            '== FORMAT ==\n'
            'Style: like a short editorial column, 150-400 words. '
            'If there is little material, write less. Do not stretch it artificially.\n\n'
            '1) START with 1-2 lively <p> paragraphs that introduce the topic. No intro like "In this video…". '
            'Get straight to the point: the problem, the context, the main idea.\n'
            '2) USE <ul><li> lists ONLY if the video has a real list of homogeneous '
            'items: a set of steps, a list of signs, a range of products, etc. '
            'If the thought flows, leave it as a paragraph, do NOT break it into bullets. '
            'Do not make a list for the sake of a list.\n'
            '3) In lists each item is 1-3 short sentences. No nested items. '
            'Do NOT start an item with a <strong>heading</strong> — write the thought right away. '
            '<strong> emphasis in an item is at most 1-2 words, only if it is '
            'a critically important term/number/name. Most items should have no emphasis at all.\n'
            '4) <strong> in a regular paragraph is also sparing, no more than once per paragraph, '
            'only on a key concept. Italics <em> are for Latin names of plants/products.\n'
            '5) <h2> subheadings ONLY if the article has 2+ separate semantic blocks. '
            'For a single topic, no subheadings.\n'
            '6) END with a short concluding paragraph: one or two sentences that give the reader '
            'a practical takeaway. No filler like "I hope this was useful".\n\n'
            '== HARD RULES ==\n'
            '— Ukrainian only, natural, with no calques from Russian.\n'
            '— Do not invent what is not in the transcript. No links, prices or calls to buy.\n'
            '— No phrases like "In this video…", "The author says…", "As shown…".\n'
            '— Do NOT repeat the article title inside.\n'
            '— Do NOT add a video embed — it will be inserted automatically.\n'
            '— Allowed tags: <h2>, <p>, <ul>, <ol>, <li>, <strong>, <em>. Nothing else.\n'
            '— Pure HTML only, no markdown.\n\n'
            'Return JSON of the form {"html": "..."}. No text outside the JSON.'
        )

        contents = (
            f'{prompt}\n\n'
            f'Title (for context, do not repeat it in the body): {title}\n'
            f'Transcript language: {src_lang or "unknown"}\n\n'
            f'Transcript:\n{clipped}'
        )
        data = llm.text_to_json(contents)
        return (data.get('html') or '').strip()
    except Exception as e:
        logger.warning('Article generation failed: %s', e)
        return ''
