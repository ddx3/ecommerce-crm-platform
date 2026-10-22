"""Upload sticker sheets (printouts) into packagings.

  python manage.py import_printouts --dir "./printouts"            # report
  python manage.py import_printouts --dir ... --apply                            # upload the confident ones
  python manage.py import_printouts --dir ... --apply --replace                  # re-upload existing ones
  python manage.py import_printouts --dir ... --apply --only "pekacid 100 g.pdf"  # targeted

Matching is by the CONTENT of the sticker (see catalog/printout_match). Only unambiguous
matches are linked automatically; ambiguous and ownerless ones are printed as a list
and saved to a report alongside, so the queue of unresolved items stays visible.

--only means «I have looked at exactly these files»: together with --apply they are linked
even from the "needs review" queue. Without --only only the unambiguous ones are
written.
"""
import json
import os

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from catalog.models import Product, ProductVariant, VariantPrintout
from catalog import printout_match as pm

REPORT = '_import_report.json'


class Command(BaseCommand):
    help = 'Matches printout files to packagings by sticker content and uploads them.'

    def add_arguments(self, parser):
        parser.add_argument('--dir', required=True, help='Folder with printout files')
        parser.add_argument('--apply', action='store_true', help='Write (without the flag, report only)')
        parser.add_argument('--replace', action='store_true',
                            help='Overwrite packagings that already have a printout')
        parser.add_argument('--only', action='append', default=[],
                            help='Process only the given files (may be repeated). '
                                 'Together with --apply it also links what ended up in "needs review".')
        parser.add_argument('--include-drop', action='store_true',
                            help='Do not skip dropshipping packagings (skipped by default)')
        parser.add_argument('--all-packaging', action='store_true',
                            help='Search among ALL packagings, not only own packaging (by default only own)')

    def handle(self, *args, **opts):
        directory = opts['dir']
        if not os.path.isdir(directory):
            raise CommandError(f'No such folder: {directory}')

        labels = pm.scan(directory)
        if opts['only']:
            wanted = set(opts['only'])
            labels = [l for l in labels if l.name in wanted]
            missing = wanted - {l.name for l in labels}
            if missing:
                raise CommandError('No such files: ' + ', '.join(sorted(missing)))
        if not labels:
            raise CommandError('No .pdf/.docx files found in the folder')

        qs = ProductVariant.objects.select_related('product').all()
        if not opts['all_packaging']:
            # A sticker sheet goes into OUR packaging; the factory pack comes with its own
            # insert. Without this narrowing, factory twins of the same
            # product (YaraTera CALCINIT, plain magnesium sulfate, etc.) cause
            # massive ambiguity: on prod 13 unambiguous matches instead of 80.
            qs = qs.filter(product__packaging_kind=Product.PACKAGING_OWN)
        if not opts['include_drop']:
            # Dropshipping is not physically packed by us — there is no insert there.
            qs = qs.exclude(is_dropshipping=True, is_hybrid=False)
        keys = [pm.variant_key(v) for v in qs]
        by_pk = {v.pk: v for v in qs}
        idf = pm.build_idf(keys)

        buckets = {'auto': [], 'review': [], 'ambiguous': [], 'none': []}
        for label in labels:
            state, cands = pm.decide(label, keys, idf)
            buckets[state].append((label, cands))

        self._idf = idf
        self._report(buckets, by_pk)

        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                '\nThis is a report only. To upload the confident matches, add --apply'))
            self._dump(directory, buckets)
            return

        # A file named explicitly has already been reviewed by the operator — so we write it
        # even if it "needs review". Without --only only the unambiguous ones remain.
        written, skipped = self._write(buckets, by_pk, opts['replace'],
                                       include_review=bool(opts['only']))
        self._dump(directory, buckets)
        self.stdout.write(self.style.SUCCESS(f'\nUploaded: {written}. Skipped (already present): {skipped}.'))
        left = len(buckets['review']) + len(buckets['ambiguous']) + len(buckets['none'])
        if left:
            self.stdout.write(self.style.WARNING(
                f'Left to sort out by hand: {left} — see {REPORT} in the folder.'))

    # ------------------------------------------------------------------ output

    def _line(self, label, variant):
        mark = '+' if variant.is_active else '-'
        return (f'    -> V{variant.pk} sku={variant.sku} {mark} stock={variant.stock} '
                f'| {variant.name}')

    def _why(self, label, cands):
        """Why the match did not go to auto-link — so the obvious is not mixed with the doubtful."""
        if label.needs_review:
            return 'content not read, matched by file name'
        if cands[0][0] < pm.AUTO_MIN:
            return f'weak match, score {cands[0][0]}'
        anchors = pm.unmatched_anchors(label, cands[0][1], self._idf)
        if anchors:
            return 'the header has a word that is not in the packaging name: ' + ', '.join(anchors)
        return 'needs checking'

    def _report(self, buckets, by_pk):
        w = self.stdout.write
        w(self.style.SUCCESS(f"\n== UNAMBIGUOUS ({len(buckets['auto'])}) =="))
        for label, cands in buckets['auto']:
            w(f'  {label.name}   [{label.source}]')
            w(self._line(label, by_pk[cands[0][1].pk]))

        if buckets['review']:
            w(self.style.WARNING(f"\n== NEEDS REVIEW ({len(buckets['review'])}) =="))
            for label, cands in buckets['review']:
                w(f'  {label.name}   ({self._why(label, cands)})')
                w(self._line(label, by_pk[cands[0][1].pk]))

        if buckets['ambiguous']:
            w(self.style.WARNING(f"\n== AMBIGUOUS ({len(buckets['ambiguous'])}) =="))
            for label, cands in buckets['ambiguous']:
                w(f'  {label.name}   size={label.grams} NPK={sorted(label.npk) or "—"} '
                  f'duration={sorted(label.duration) or "—"}')
                for value, key in cands[:5]:
                    w(f'      {value}' + self._line(label, by_pk[key.pk])[4:])

        if buckets['none']:
            w(self.style.ERROR(f"\n== NOWHERE TO ATTACH ({len(buckets['none'])}) =="))
            for label, cands in buckets['none']:
                best = f'{cands[0][0]} {cands[0][1].name}' if cands else '—'
                w(f'  {label.name}   best: {best}')

    def _dump(self, directory, buckets):
        data = {
            state: [{
                'file': label.name,
                'source': label.source,
                'label': label.lines,
                'grams': label.grams,
                'candidates': [{'score': v, 'variant': k.pk, 'sku': k.sku, 'name': k.name}
                               for v, k in cands[:5]],
            } for label, cands in items]
            for state, items in buckets.items()
        }
        path = os.path.join(directory, REPORT)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        self.stdout.write(f'\nReport: {path}')

    # ------------------------------------------------------------------ writing

    def _write(self, buckets, by_pk, replace, include_review=False):
        # One sheet may exist both as .docx and already converted to .pdf
        # (magnesium sulfate 500 g). The field is single, so we take the PDF; otherwise the second
        # iteration would silently overwrite the first, depending on the folder read order.
        best = {}
        writable = buckets['auto'] + (buckets['review'] if include_review else [])
        for label, cands in writable:
            pk = cands[0][1].pk
            rival = best.get(pk)
            if rival is None or (label.name.lower().endswith('.pdf')
                                 and not rival.name.lower().endswith('.pdf')):
                if rival is not None:
                    self.stdout.write(f'  ~ {rival.name}: skipped in favour of {label.name}')
                best[pk] = label
            else:
                self.stdout.write(f'  ~ {label.name}: skipped in favour of {rival.name}')

        written = skipped = 0
        for pk, label in best.items():
            variant = by_pk[pk]
            rows = VariantPrintout.objects.filter(variant=variant)
            # Do not add the same sheet a second time: otherwise every run of the command
            # would breed duplicates, because a packaging can now have several sheets.
            same = [r for r in rows if r.basename == label.name.replace(' ', '_')
                    or r.basename == label.name]
            if same and not replace:
                skipped += 1
                continue
            if replace:
                for r in rows:
                    r.delete()          # django_cleanup will remove the file and the preview
            with open(label.path, 'rb') as fh:
                VariantPrintout.objects.create(variant=variant, file=File(fh, name=label.name))
            written += 1
        return written, skipped
