"""Photos in a bundle description draft (catalog/description_images.py)."""
import base64
import io
import json
import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from catalog import description_images as di


def _png_uri(w=40, h=30, mode='RGB'):
    buf = io.BytesIO()
    Image.new(mode, (w, h), (200, 30, 30) if mode == 'RGB' else (200, 30, 30, 0)).save(buf, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


def _img(src, alt=''):
    return f'<img src="{src}" alt="{alt}">'


class ExtractImagesTests(SimpleTestCase):
    def test_text_without_images_is_untouched(self):
        html = '<p>Text &nbsp; without photos</p>'
        self.assertEqual(di.extract_images(html), (html, [], []))

    def test_embedded_photo_becomes_mark_and_small_jpeg(self):
        text, drafts, notes = di.extract_images(f'<p>Before</p><p>{_img(_png_uri(3000, 1000))}</p>')
        self.assertEqual(text, '<p>Before</p><p>[[PHOTO 1]]</p>')
        self.assertEqual(notes, [])
        self.assertEqual(len(drafts), 1)
        im = Image.open(io.BytesIO(drafts[0].jpeg))
        self.assertEqual(im.format, 'JPEG')
        self.assertLessEqual(max(im.size), di.MAX_SIDE)

    def test_transparent_png_gets_white_background(self):
        _, drafts, _ = di.extract_images(_img(_png_uri(10, 10, 'RGBA')))
        px = Image.open(io.BytesIO(drafts[0].jpeg)).getpixel((5, 5))
        self.assertTrue(all(c > 240 for c in px), px)

    def test_saved_media_photo_is_kept_without_vision(self):
        text, drafts, _ = di.extract_images(f'<p>{_img("/media/bundle_description/ab/x.jpg", "Bush")}</p>')
        self.assertEqual(text, '<p>[[PHOTO 1]]</p>')
        self.assertEqual((drafts[0].kind, drafts[0].url, drafts[0].jpeg), ('photo', '/media/bundle_description/ab/x.jpg', None))

    def test_unsafe_media_src_is_not_trusted_and_svg_counts_as_unreadable(self):
        evil = '/media/x.jpg" onerror="alert(1)'
        svg = 'data:image/svg+xml;base64,PHN2Zy8+'
        text, drafts, notes = di.extract_images(f"<p><img src='{evil}'>{_img(svg)}</p>")
        self.assertEqual((text, drafts), ('<p></p>', []))
        self.assertEqual(len(notes), 2)

    def test_external_and_unreadable_are_dropped_with_notes(self):
        bad = 'data:image/png;base64,AAAA'
        text, drafts, notes = di.extract_images(f'<p>{_img("https://shop.example/a.jpg")}{_img(bad)}Text</p>')
        self.assertEqual((text, drafts), ('<p>Text</p>', []))
        self.assertEqual(len(notes), 2)
        self.assertIn('given as a link', notes[0])
        self.assertIn('Could not read', notes[1])

    def test_limit_of_embedded_images(self):
        uri = _png_uri()
        with mock.patch.object(di, 'MAX_IMAGES', 2):
            text, drafts, notes = di.extract_images(''.join(f'<p>{_img(uri)}</p>' for _ in range(3)))
        self.assertEqual(len(drafts), 2)
        self.assertEqual(text.count('[[PHOTO'), 2)
        self.assertIn('extra ones (1)', notes[0])


class CleanTablesTests(SimpleTestCase):
    def test_thead_th_flattened_and_header_bold(self):
        out = di.clean_tables(
            '<table style="x"><thead><tr><th>Month</th><th>Dose</th></tr></thead>'
            '<tbody><tr><td class="c">May</td><td>25 ml</td></tr></tbody></table>')
        self.assertEqual(out, '<table><tr><td><strong>Month</strong></td><td><strong>Dose</strong></td></tr>'
                              '<tr><td>May</td><td>25 ml</td></tr></table>')

    def test_bold_cells_stay_bold_and_inner_markup_is_flattened(self):
        out = di.clean_tables('<table><tr><td><strong>Header</strong></td><td><p>a <em>b</em></p></td></tr></table>')
        self.assertEqual(out, '<table><tr><td><strong>Header</strong></td><td>a b</td></tr></table>')

    def test_colspan_becomes_empty_cells_not_duplicates(self):
        out = di.clean_tables('<table><tr><td colspan="3">Scheme</td></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>')
        self.assertEqual(out, '<table><tr><td>Scheme</td><td></td><td></td></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>')

    def test_empty_table_is_removed_and_text_untouched(self):
        self.assertEqual(di.clean_tables('<p>a</p><table></table><p>b</p>'), '<p>a</p><p>b</p>')
        self.assertEqual(di.clean_tables('<p>no table here</p>'), '<p>no table here</p>')

    def test_digits_are_never_altered(self):
        out = di.clean_tables('<table><tr><td>0,25 l</td><td>1:1000</td><td>+12°C</td></tr></table>')
        self.assertIn('0,25 l', out)
        self.assertIn('1:1000', out)
        self.assertIn('+12°C', out)


def _draft(n, kind, **kw):
    d = di.DraftImage(n, jpeg=b'jpeg-bytes-%d' % n)
    d.kind = kind
    for k, v in kw.items():
        setattr(d, k, v)
    return d


class MergeIntoTextTests(SimpleTestCase):
    def test_table_replaces_mark_without_nesting_in_paragraph(self):
        d = _draft(1, 'table', html='<table><tr><td>1</td></tr></table>')
        self.assertEqual(di.merge_into_text('<p>Before</p><p>[[PHOTO 1]]</p><p>After</p>', [d]),
                         '<p>Before</p><table><tr><td>1</td></tr></table><p>After</p>')

    def test_text_lines_become_escaped_paragraphs(self):
        d = _draft(1, 'text', text='Plant Booster — 25 ml\n\nA < B & C')
        self.assertEqual(di.merge_into_text('<p>[[PHOTO 1]]</p>', [d]),
                         '<p>Plant Booster — 25 ml</p><p>A &lt; B &amp; C</p>')

    def test_photo_keeps_mark_and_other_disappears(self):
        text = di.merge_into_text('<p>[[PHOTO 1]]</p><p>x [[PHOTO 2]] y</p><p>[[PHOTO 3]]</p>',
                                  [_draft(1, 'photo'), _draft(2, 'other'), _draft(3, 'other')])
        self.assertEqual(text, '<p>[[PHOTO 1]]</p><p>x  y</p>')

    def test_table_caption_becomes_bold_paragraph_above_the_table(self):
        d = _draft(1, 'table', html='<table><tr><td>1</td></tr></table>', caption='Scheme <treatments>')
        self.assertEqual(di.merge_into_text('<p>[[PHOTO 1]]</p>', [d]),
                         '<p><strong>Scheme &lt;treatments&gt;</strong></p><table><tr><td>1</td></tr></table>')

    def test_inline_mark_gets_table_in_place(self):
        d = _draft(1, 'table', html='<table><tr><td>1</td></tr></table>')
        self.assertEqual(di.merge_into_text('<p>before [[PHOTO 1]] after</p>', [d]),
                         '<p>before <table><tr><td>1</td></tr></table> after</p>')
        # Language data: the legacy Ukrainian spelling of the mark ([[ФОТО n]]) is still accepted when parsing.
        self.assertEqual(di.merge_into_text('<p>before [[ФОТО 1]] after</p>', [d]),
                         '<p>before <table><tr><td>1</td></tr></table> after</p>')


class ClassifyTests(SimpleTestCase):
    def _classify(self, drafts, answer=None, side_effect=None):
        with mock.patch('core.llm.images_to_json', return_value=answer, side_effect=side_effect) as m:
            di.classify(drafts)
        return m

    def test_kinds_applied(self):
        drafts = [_draft(1, ''), _draft(2, ''), _draft(3, ''), _draft(4, '')]
        m = self._classify(drafts, {'images': [
            {'n': 1, 'kind': 'photo', 'alt': 'Leaf <b>rose</b>'},
            {'n': 2, 'kind': 'table', 'html': '<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>'},
            {'n': 3, 'kind': 'text', 'text': ' Plant Booster — 25 ml '},
            {'n': 4, 'kind': 'other'},
        ]})
        self.assertEqual([d.kind for d in drafts], ['photo', 'table', 'text', 'other'])
        self.assertEqual(drafts[0].alt, 'Leaf rose')
        self.assertEqual(drafts[1].html, '<table><tr><td><strong>A</strong></td></tr><tr><td>1</td></tr></table>')
        self.assertEqual(drafts[2].text, 'Plant Booster — 25 ml')
        self.assertEqual(len(m.call_args.args[0]), 4)   # all photos in one call

    def test_unusable_answers_fall_back_to_photo_never_lose_the_image(self):
        drafts = [_draft(1, ''), _draft(2, ''), _draft(3, ''), _draft(4, '')]
        self._classify(drafts, {'images': [
            {'n': 1, 'kind': 'table', 'html': '<p>not a table</p>'},
            {'n': 2, 'kind': 'text', 'text': '  '},
            {'n': 3, 'kind': 'wat'},
            # n=4 the model skipped
        ]})
        self.assertEqual([d.kind for d in drafts], ['photo'] * 4)
        self.assertTrue(all(d.note for d in drafts))

    def test_already_saved_photos_are_not_sent_to_vision(self):
        saved = di.DraftImage(1, url='/media/x.jpg')
        new = _draft(2, '')
        m = self._classify([saved, new], {'images': [{'n': 1, 'kind': 'other'}]})
        self.assertEqual(len(m.call_args.args[0]), 1)
        self.assertEqual((saved.kind, new.kind), ('photo', 'other'))

    def test_no_new_images_means_no_llm_call(self):
        with mock.patch('core.llm.images_to_json') as m:
            di.classify([di.DraftImage(1, url='/media/x.jpg')])
        m.assert_not_called()

    def test_bad_json_is_retried_once(self):
        drafts = [_draft(1, '')]
        m = self._classify(drafts, side_effect=[ValueError('no JSON'), {'images': [{'n': 1, 'kind': 'other'}]}])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(drafts[0].kind, 'other')

    def test_persistent_failure_raises_instead_of_dropping_photos(self):
        with self.assertRaises(di.DescriptionImageError):
            self._classify([_draft(1, '')], side_effect=ValueError('no JSON'))
        with self.assertRaises(di.DescriptionImageError) as cm:
            self._classify([_draft(1, '')], side_effect=RuntimeError('Timeout 120s'))
        self.assertIn('Timeout', str(cm.exception))


class FinishTests(SimpleTestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        override = override_settings(MEDIA_ROOT=self.media, MEDIA_URL='/media/')
        override.enable()
        self.addCleanup(override.disable)

    def _prepared(self, *drafts):
        return di.Prepared('', list(drafts), [])

    def test_mark_becomes_img_with_media_url_and_file_is_stored_once(self):
        d = _draft(1, 'photo', alt='Leaf')
        desc, norms, notes = di.finish(self._prepared(d), '<p>a</p><p>[[PHOTO 1]]</p><p>b</p>', '')
        self.assertRegex(desc, r'<p>a</p><p><img src="/media/bundle_description/[0-9a-f]{2}/[0-9a-f]{40}\.jpg" alt="Leaf"></p><p>b</p>')
        self.assertNotIn('data:', desc)
        self.assertEqual(notes, [])        # success — no summaries
        # re-running the same photo — the same file, no duplicates
        d2 = _draft(1, 'photo')
        di.finish(self._prepared(d2), '[[PHOTO 1]]', '')
        self.assertEqual(d.url, d2.url)

    def test_existing_media_photo_is_reused_not_reuploaded(self):
        d = di.DraftImage(1, url='/media/bundle_description/ab/x.jpg', alt='')
        desc, _, _ = di.finish(self._prepared(d), '<p>[[PHOTO 1]]</p>', '')
        self.assertEqual(desc, '<p><img src="/media/bundle_description/ab/x.jpg" alt=""></p>')

    def test_duplicate_and_unknown_marks_are_removed(self):
        desc, norms, _ = di.finish(self._prepared(_draft(1, 'photo')), '<p>[[PHOTO 1]]</p><p>[[PHOTO 9]]</p>', '<p>[[PHOTO 1]]</p>')
        self.assertEqual(desc.count('<img'), 1)
        self.assertEqual(norms, '<p></p>')
        self.assertNotIn('[[', desc)

    def test_photo_the_model_dropped_is_added_before_the_fixed_last_line(self):
        last = '<p>The bundle includes a printed instruction with a detailed description of all treatments.</p>'
        desc, _, _ = di.finish(self._prepared(_draft(1, 'photo')), '<p>Description</p>' + last, '')
        self.assertTrue(desc.startswith('<p>Description</p><p><img '))
        self.assertTrue(desc.endswith(last))

    def test_lost_photo_goes_to_norms_when_no_description_and_is_reported_when_nowhere(self):
        _, norms, _ = di.finish(self._prepared(_draft(1, 'photo')), '', '<p>Norms</p>')
        self.assertIn('<img ', norms)
        d = _draft(1, 'photo')
        desc, norms, notes = di.finish(self._prepared(d), '', '')
        self.assertEqual((desc, norms, d.url), ('', '', ''))     # goes nowhere — and the file isn't saved
        self.assertTrue(any('not placed' in n for n in notes))

    def test_tables_are_normalised_and_success_has_no_notes(self):
        prepared = self._prepared(_draft(1, 'table'), _draft(2, 'text'), _draft(3, 'other'))
        desc, _, notes = di.finish(prepared, '<table><thead><tr><th>A</th></tr></thead></table>', '')
        self.assertEqual(desc, '<table><tr><td><strong>A</strong></td></tr></table>')
        self.assertEqual(notes, [])        # no summaries and no "please verify" requests

    def test_without_images_only_tables_are_normalised(self):
        desc, norms, notes = di.finish(di.Prepared('', [], []), '<p>x</p>', '<p>y</p>')
        self.assertEqual((desc, norms, notes), ('<p>x</p>', '<p>y</p>', []))


@override_settings(ALLOWED_HOSTS=['testserver'])
class GenerateDescriptionViewTests(TestCase):
    """Glue in generate_description: photos → vision → text for the model → markers in the response."""

    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        override = override_settings(MEDIA_ROOT=self.media, MEDIA_URL='/media/')
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_login(get_user_model().objects.create(email='img@example.com', is_staff=True, is_superuser=True))

    def _post(self, text):
        return self.client.post('/admin/catalog/generate-description/',
                                data=json.dumps({'text': text, 'prompt': 'bundle_items'}),
                                content_type='application/json')

    def test_photos_are_classified_merged_and_restored(self):
        html = (f'<p>Bundle description</p><p>{_img(_png_uri())}</p><p>{_img(_png_uri(50, 50))}</p>')
        vision = {'images': [{'n': 1, 'kind': 'table', 'html': '<table><tr><th>Month</th><th>Dose</th></tr><tr><td>May</td><td>25 ml</td></tr></table>'},
                             {'n': 2, 'kind': 'photo', 'alt': 'Package'}]}
        model = {'general_name': 'T', 'name': 'Bundle "T"', 'variant_name': 'Bundle "T"', 'items': [], 'category': [],
                 'description': '<p>Description</p><p>[[PHOTO 2]]</p>', 'application_norms': '<table><thead><tr><th>Month</th></tr></thead></table>'}
        with mock.patch('core.llm.images_to_json', return_value=vision) as vis, \
                mock.patch('core.llm.text_to_json', return_value=model) as txt, \
                mock.patch('google.genai.Client'), \
                mock.patch('catalog.views._match_bundle_items_to_variants', return_value=[]):
            resp = self._post(html)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        sent = txt.call_args.args[0]
        self.assertNotIn('data:image', sent)                         # base64 doesn't reach the model
        self.assertIn('<table><tr><td><strong>Month</strong></td><td><strong>Dose</strong></td></tr>', sent)
        self.assertIn('[[PHOTO 2]]', sent)
        self.assertEqual(len(vis.call_args.args[0]), 2)
        self.assertRegex(body['description'], r'<p>Description</p><p><img src="/media/bundle_description/\w\w/\w{40}\.jpg" alt="Package"></p>')
        self.assertEqual(body['application_norms'], '<table><tr><td><strong>Month</strong></td></tr></table>')
        self.assertEqual(body['image_notes'], [])                   # success is silence, the form will say "Done"

    def test_vision_failure_returns_error_and_never_calls_the_main_model(self):
        with mock.patch('core.llm.images_to_json', side_effect=RuntimeError('Timeout 120s')), \
                mock.patch('core.llm.text_to_json') as txt:
            resp = self._post(f'<p>Text</p><p>{_img(_png_uri())}</p>')
        self.assertEqual(resp.status_code, 500)
        self.assertIn('Could not parse the photos', resp.json()['error'])
        txt.assert_not_called()

    def test_only_decorative_photo_and_no_text_is_rejected(self):
        with mock.patch('core.llm.images_to_json', return_value={'images': [{'n': 1, 'kind': 'other'}]}), \
                mock.patch('core.llm.text_to_json') as txt:
            resp = self._post(f'<p>{_img(_png_uri())}</p>')
        self.assertEqual(resp.status_code, 400)
        txt.assert_not_called()

    def test_text_without_images_does_not_touch_vision(self):
        model = {'general_name': '', 'name': '', 'variant_name': '', 'items': [], 'category': [], 'description': '<p>x</p>'}
        with mock.patch('core.llm.images_to_json') as vis, \
                mock.patch('core.llm.text_to_json', return_value=model), \
                mock.patch('google.genai.Client'), \
                mock.patch('catalog.views._match_bundle_items_to_variants', return_value=[]):
            resp = self._post('<p>Just text</p>')
        self.assertEqual(resp.json()['image_notes'], [])
        vis.assert_not_called()
