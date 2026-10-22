"""Fills category meta_title / meta_description with SEO texts matching the semantics
(the SEO keyword research). Idempotent: by default writes only to empty fields,
with --force overwrites. Portable: run locally and on prod (same slugs).

    python manage.py seo_apply_category_meta            # empty ones only
    python manage.py seo_apply_category_meta --force    # overwrite everything
    python manage.py seo_apply_category_meta --dry-run  # show without writing
"""
from django.core.management.base import BaseCommand

from catalog.models import Category

# slug -> (meta_title, meta_description). Texts target the commercial queries of the semantics
# + USP (delivery across Ukraine). Unique, no duplicates.
META = {
    # ── Root ──────────────────────────────────────────────────────────────────
    'zasobi-zakhistu-roslin': (
        'Plant protection products — insecticides, fungicides, herbicides',
        'Plant protection products against pests, diseases and weeds: insecticides, '
        'fungicides, herbicides, biological products. Original products, delivery across Ukraine.'),
    'dobriva-stimuliatori': (
        'Fertilizers and plant growth stimulants — buy in Ukraine',
        'Mineral and organic fertilizers, growth stimulants, anti-stress products and rooting agents '
        'for the garden and vegetable plot. Feeding for tomatoes, roses, conifers. Delivery across Ukraine.'),
    'agrovolokno': (
        'White and black agrofibre — buy by density',
        'Covering agrofibre (white) against frost and mulching agrofibre (black) against weeds, '
        'density 17–50 g/m². For strawberries, greenhouses, garden beds. Delivery across Ukraine.'),
    'agrotkanina-shpilki': (
        'Mulching agrotextile and fixing pegs',
        'Weed-barrier agrotextile for mulching garden beds and strawberries, and pegs '
        'for secure fixing. Strong, durable. Delivery across Ukraine.'),
    'bordiuri-sadovi': (
        'Garden borders and border tape for garden beds',
        'Garden borders and border tape for beds, flower beds and paths. Plastic fences '
        'and stakes for fixing. Neat beds made easy. Delivery across Ukraine.'),
    'dlia-polivu': (
        'Bradas connectors, adapters and fittings for watering',
        'Bradas connectors, adapters and fittings for garden hoses and drip irrigation. '
        'Aqua-stop connections, leak-free. Delivery across Ukraine.'),
    'odiag-ta-aksesuari': (
        'Garden gloves, hats and accessories',
        'Leather and Womanswork garden gloves, sun hats, tool holsters and covers, '
        'belts. Comfort in the garden. Delivery across Ukraine.'),
    'sadovii-inventar': (
        'Garden tools: ARS pruners, saws, rakes',
        'Professional garden tools: Japanese ARS pruners and saws, hedge trimmers, rakes, '
        'sharpening stones. Sharp and reliable. Delivery across Ukraine.'),
    'sitka-dlia-zatinennia': (
        'Protective shade netting against sun and birds',
        'Shade netting for greenhouses, gardens and vegetable plots — protection from sun, birds and hail. '
        'Different shading densities. Delivery across Ukraine.'),
    'domashni-limoni': (
        'Meyer lemon saplings — indoor lemon',
        'Meyer indoor lemon saplings — fragrant, sweet-and-sour, fruiting all year '
        'round. Easy care. Sapling delivery across Ukraine by Nova Poshta.'),
    'komplekti': (
        'Plant care bundles — ready-made sets',
        'Ready-made bundles of fertilizers and protection products for roses, hydrangeas, conifers, fruit trees '
        'and lawns. Everything for the season in one set. Delivery across Ukraine.'),

    # ── Plant protection products (subcategories) ─────────────────────────────
    'insektitsidi-ta-akaritsidi': (
        'Insecticides and acaricides against pests — buy',
        'Insecticides and acaricides against Colorado beetle, aphids, mites, whiteflies. '
        'Actara, Actellic, Ampligo and others. Original products, delivery across Ukraine.'),
    'fungitsidi': (
        'Fungicides against plant diseases — late blight, mildew',
        'Fungicides against late blight, powdery mildew, scab and rust for tomatoes, grapes, '
        'roses. Ridomil, Skor, Horus. Delivery across Ukraine.'),
    'biopreparati': (
        'Biological products for plant protection — biocontrol',
        'Biological products for organic plant protection against diseases and pests: Fitosporin, '
        'Trichodermin, Aktofit. Safe for people and bees. Delivery across Ukraine.'),
    'gerbitsidi': (
        'Total-action herbicides against weeds — buy',
        'Total-action herbicides against weeds: Roundup, Hurricane, Napalm, Broker. '
        'Destroy annual and perennial weeds. Delivery across Ukraine.'),
    'protruiniki': (
        'Seed and potato dressings — buy',
        'Dressings for treating seeds, potatoes and grain before planting — protect seedlings '
        'from diseases and pests from day one. Delivery across Ukraine.'),
    'prilipachi-adiuvanti-reguliatori': (
        'Stickers and adjuvants for spraying',
        'Stickers, adjuvants and regulators for effective spraying — the product '
        'stays on the leaves longer and is not washed off by rain. Delivery across Ukraine.'),
    'obrobka-zriziv': (
        'Garden pitch and cut sealing products',
        'Garden pitch and putties for sealing cuts after pruning trees and shrubs — protect wounds '
        'from infection and drying out. Delivery across Ukraine.'),

    # ── Fertilizers (subcategories) ───────────────────────────────────────────
    'dobriva': (
        'Fertilizers for the garden and vegetable plot — mineral, complex',
        'Fertilizers for tomatoes, cucumbers, roses, strawberries and conifers: complex, mineral, '
        'chelated micronutrient fertilizers, saltpeter. Feeding for the whole season. Delivery across Ukraine.'),
    'stimuliatori-antistresanti': (
        'Plant growth stimulants and anti-stress products',
        'Growth stimulants, biostimulants and anti-stress products for plants — better growth, '
        'flowering and harvest, recovery after stress. Delivery across Ukraine.'),
    'ukoriniuvachi': (
        'Rooting agents and root growth stimulants',
        'Rooting agents for cuttings and seedlings: Kornevin and other root growth stimulants — '
        'fast rooting even for difficult crops. Delivery across Ukraine.'),
    'biodobriva': (
        'Organic biofertilizers for plants — buy',
        'Organic-based biofertilizers for natural plant feeding and soil fertility. '
        'Safe for the garden, vegetable plot and bees. Delivery across Ukraine.'),
    'dobriva-prolongovanoi-dii': (
        'Slow-release fertilizers — long-lasting feeding',
        'Slow-release (long-acting) fertilizers — feed plants gradually for months with a single '
        'application. Convenient for the garden and containers. Delivery across Ukraine.'),
    'dobriva-super-simeinii-sad': (
        'Super (Family Garden) fertilizers — buy',
        'Super "Family Garden" line fertilizers for vegetables, berries and flowers — balanced '
        'feeding for a rich harvest. Delivery across Ukraine.'),

    # ── Agrotextile (subcategories) ───────────────────────────────────────────
    'agrotkanina': (
        'Weed-barrier mulching agrotextile — buy',
        'Weed-barrier mulching agrotextile for garden beds, strawberries and paths — strong, '
        'durable (5–7 years). Different densities. Delivery across Ukraine.'),
    'shpilki': (
        'Pegs and stakes for agrofibre and agrotextile',
        'Metal and plastic U-shaped pegs (stakes) for fixing agrofibre and '
        'agrotextile to the ground. Reusable. Delivery across Ukraine.'),

    # ── Garden tools (subcategories) ──────────────────────────────────────────
    'sekatori-kushchorizi-nozhitsi': (
        'ARS pruners, hedge trimmers and garden shears',
        'Japanese ARS pruners, hedge trimmers and garden shears — sharp, reliable, for pruning '
        'trees, shrubs and roses. Original. Delivery across Ukraine.'),
    'pilki': (
        'ARS folding garden saws — buy',
        'Japanese ARS garden saws — folding and fixed, for pruning branches and knots. '
        'Sharp cut, durability. Delivery across Ukraine.'),
    'zasobi-po-dogliadu-za-instrumentom': (
        'Garden tool care — sharpening stones',
        'Garden tool care products: sharpening stones, lubricants and products for '
        'sharpening pruners and knives. Delivery across Ukraine.'),
    'dlia-roboti-z-zemleiu': (
        'Soil tools — rakes, hoes',
        'Garden tools for working the soil: rakes, hoes, trowels, cultivators. Convenient '
        'and sturdy tools for the vegetable plot. Delivery across Ukraine.'),
    'koshiki-kilimki': (
        'Garden baskets and work mats',
        'Garden baskets for harvesting and kneeling mats for working. Comfort '
        'in the garden and vegetable plot. Delivery across Ukraine.'),
    'shpagati-kembriki': (
        'Twine and tying tape for tying up plants',
        'Twine and tying tape for tying up tomatoes, grapes and shrubs — reliable fixing '
        'without damaging the stems. Delivery across Ukraine.'),
    'inshe': (
        'Garden tools and accessories — other',
        'Useful garden tools and accessories for the garden and vegetable plot. Quality tools for '
        'everyday work. Delivery across Ukraine.'),

    # ── Clothing and accessories (subcategories) ──────────────────────────────
    'panami': (
        'Sun hats for the garden',
        'Sun hats for working in the garden — protection from the heat, comfortable and light, '
        'including convertible ones. Delivery across Ukraine.'),
    'rukavitsi': (
        'Leather and work garden gloves — buy',
        'Garden gloves — leather, for roses and work with thorny plants. Hand protection and '
        'comfort in the garden. Delivery across Ukraine.'),
    'tovari-womanswork': (
        'Womanswork products — garden gloves and accessories',
        "Womanswork products (USA): women's garden gloves with long cuffs and premium-quality "
        'accessories. Original. Delivery across Ukraine.'),
    'chokhli-koshiki-dlia-instrumentiv': (
        'Covers and baskets for garden tools',
        'Covers, holsters and baskets for storing and carrying garden tools — '
        'pruners, saws, knives. Delivery across Ukraine.'),
    'shkiriani-virobi': (
        'Leather goods for the garden — belts, holsters, bags',
        'Leather goods for the garden: tool belts, pruner holsters, bags. '
        'Quality leather, durability. Delivery across Ukraine.'),

    # ── Bundles (subcategories — sets per plant) ──────────────────────────────
    'gortenziia': (
        'Hydrangea bundle — fertilizers and care',
        'Ready-made bundle of fertilizers and products for hydrangeas — acidification, feeding and rich '
        'bloom color. Everything for the season. Delivery across Ukraine.'),
    'troiandi': (
        'Rose bundle — fertilizers and protection',
        'Ready-made bundle for roses: fertilizers for abundant flowering and protection products against '
        'diseases and pests. Delivery across Ukraine.'),
    'khvoini': (
        'Conifer bundle — fertilizers and care',
        'Ready-made bundle for conifers: feeding, protection against needle browning and '
        'growth stimulation. Delivery across Ukraine.'),
    'samshit': (
        'Boxwood bundle — protection and feeding',
        'Ready-made bundle for boxwood: protection against box tree moth and diseases, and fertilizers for dense '
        'foliage. Delivery across Ukraine.'),
    'plodovi': (
        'Fruit tree bundle — care for the whole season',
        'Ready-made bundle for fruit trees: feeding and protection against diseases and pests for the '
        'whole season. Delivery across Ukraine.'),
    'iagidni': (
        'Berry bundle — strawberries, raspberries',
        'Ready-made bundle for berry crops (strawberries, raspberries, currants): fertilizers and '
        'protection for a rich harvest. Delivery across Ukraine.'),
    'gazon': (
        'Lawn bundle — fertilizers and care',
        'Ready-made bundle for lawns: fertilizers for dense green growth and products against weeds '
        'and moss. A well-kept lawn made easy. Delivery across Ukraine.'),
    'bio-dogliad': (
        'Bio plant care bundle',
        'Ready-made bundle for biological plant care — organic feeding and protection, '
        'safe for people and bees. Delivery across Ukraine.'),
    'kviti-v-konteinerakh': (
        'Container flower bundle — fertilizers and care',
        'Ready-made bundle of fertilizers and care products for flowers and plants in containers, '
        'pots and planters — feeding and abundant flowering all season. Delivery across Ukraine.'),
}


class Command(BaseCommand):
    help = 'Fills category meta_title/meta_description with SEO texts matched to the keyword semantics.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Overwrite non-empty fields')
        parser.add_argument('--dry-run', action='store_true', help='Show without writing')

    def handle(self, *args, **opts):
        force, dry = opts['force'], opts['dry_run']
        written = skipped = missing = 0
        for slug, (title, desc) in META.items():
            cat = Category.objects.filter(slug=slug).first()
            if not cat:
                self.stderr.write(f'  ! no such category: {slug}')
                missing += 1
                continue
            if (cat.meta_title or cat.meta_description) and not force:
                skipped += 1
                continue
            if dry:
                self.stdout.write(f'  [dry] {slug}: {title}')
                written += 1
                continue
            cat.meta_title = title
            cat.meta_description = desc
            cat.save(update_fields=['meta_title', 'meta_description'])
            written += 1
        self.stdout.write(self.style.SUCCESS(
            f'Done: written {written}, skipped (non-empty) {skipped}, missing slugs {missing}'))
