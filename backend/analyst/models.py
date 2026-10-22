from django.db import models
from django.utils import timezone


class AnalystRun(models.Model):
    """A single Analyst run per calendar day (Europe/Kyiv).

    The unique date is a guard against a double run after a redeploy: 'once a day' in the
    scheduler is counted from the process start, here — from the calendar.
    """
    RUNNING, OK, FAILED = 'running', 'ok', 'failed'
    STATUS_CHOICES = [(RUNNING, 'Running'), (OK, 'Successful'), (FAILED, 'Error')]

    date = models.DateField(unique=True, verbose_name='Date')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=RUNNING, verbose_name='Status')
    started_at = models.DateTimeField(default=timezone.now, verbose_name='Start')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='End')
    total_count = models.PositiveIntegerField(default=0, verbose_name='Active findings')
    new_count = models.PositiveIntegerField(default=0, verbose_name='New findings')
    error = models.TextField(blank=True, default='', verbose_name='Error')

    class Meta:
        verbose_name = 'Analyst run'
        verbose_name_plural = 'Analyst runs'
        ordering = ['-date']

    def __str__(self):
        return f'{self.date} · {self.get_status_display()}'


class Finding(models.Model):
    """An Analyst finding — a card in the Analyst section.

    Lives as long as a check keeps repeating it: as soon as the check stops returning it,
    `resolved_at` is set automatically. If the finding appears again, it is reopened as new.
    The 'Done' / 'Postponed' status is a human decision, the check does not
    overwrite it.
    """
    INFO, WARN, CRITICAL = 1, 2, 3
    SEVERITY_CHOICES = [(INFO, 'Info'), (WARN, 'Warning'), (CRITICAL, 'Critical')]

    NEW, SNOOZED, DONE = 'new', 'snoozed', 'done'
    STATUS_CHOICES = [(NEW, 'New'), (SNOOZED, 'Postponed'), (DONE, 'Done')]

    SECTION_HEALTH, SECTION_PURCHASE = 'health', 'purchase'
    SECTION_CHOICES = [
        (SECTION_HEALTH, 'System health'),
        (SECTION_PURCHASE, 'Purchasing'),
    ]

    key = models.CharField(max_length=120, unique=True, verbose_name='Key')
    check_name = models.CharField(max_length=50, db_index=True, verbose_name='Check')
    section = models.CharField(max_length=10, choices=SECTION_CHOICES, verbose_name='Section')
    severity = models.PositiveSmallIntegerField(choices=SEVERITY_CHOICES, default=WARN, verbose_name='Importance')
    title = models.CharField(max_length=200, verbose_name='Title')
    body = models.TextField(blank=True, default='', verbose_name='Details')
    data = models.JSONField(default=dict, blank=True, verbose_name='Data')
    link = models.CharField(max_length=200, blank=True, default='', verbose_name='Link')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=NEW, db_index=True, verbose_name='Status')
    snoozed_until = models.DateTimeField(null=True, blank=True, verbose_name='Postponed until')

    first_seen = models.DateTimeField(default=timezone.now, verbose_name='First seen')
    last_seen = models.DateTimeField(default=timezone.now, verbose_name='Last seen')
    resolved_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name='Disappeared')

    class Meta:
        verbose_name = 'Finding'
        verbose_name_plural = 'Findings'
        ordering = ['-severity', '-first_seen']

    def __str__(self):
        return f'[{self.get_severity_display()}] {self.title}'


class ActionRun(models.Model):
    """A fix proposed for a finding: first a PLAN (an exact list of edits, nothing is written to the
    data),
    then — after the owner's 'Apply' — application. The log is needed for rollback:
    `changes` holds the before and after values of each edit.

    changes: [{model, pk, field, before, after, text}] — values are JSON-compatible (Decimal → str).
    """
    PLANNED, APPLIED, REVERTED, FAILED = 'planned', 'applied', 'reverted', 'failed'
    STATUS_CHOICES = [
        (PLANNED, 'Plan'), (APPLIED, 'Applied'), (REVERTED, 'Rolled back'), (FAILED, 'Failed'),
    ]

    finding = models.ForeignKey(
        Finding, null=True, blank=True, on_delete=models.SET_NULL, related_name='actions',
        verbose_name='Finding')
    action = models.CharField(max_length=50, verbose_name='Action')
    title = models.CharField(max_length=200, verbose_name='What was done')
    params = models.JSONField(default=dict, blank=True, verbose_name='Parameters')
    changes = models.JSONField(default=list, blank=True, verbose_name='Edits')
    notes = models.JSONField(default=list, blank=True, verbose_name='Plan notes')
    reversible = models.BooleanField(default=True, verbose_name='Can be rolled back')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PLANNED, db_index=True)
    error = models.TextField(blank=True, default='')
    created_by = models.CharField(max_length=254, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)
    applied_at = models.DateTimeField(null=True, blank=True)
    reverted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Fix'
        verbose_name_plural = 'Fixes'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} · {self.get_status_display()}'


class JobHeartbeat(models.Model):
    """Heartbeat of a scheduler background task: when it last completed without error.

    The scheduler will not report its own death (in August it stood idle for 4 days), so
    the analyst watches these marks from the outside.
    """
    name = models.CharField(max_length=60, unique=True, verbose_name='Task')
    last_ok_at = models.DateTimeField(null=True, blank=True, verbose_name='Last successful')
    last_fail_at = models.DateTimeField(null=True, blank=True, verbose_name='Last failed')
    last_error = models.CharField(max_length=300, blank=True, default='', verbose_name='Last error')

    class Meta:
        verbose_name = 'Task heartbeat'
        verbose_name_plural = 'Task heartbeats'
        ordering = ['name']

    def __str__(self):
        return self.name


class PlannedPurchase(models.Model):
    """A purchase the owner has already ordered from the supplier, but the goods have not arrived yet.

    The purchasing calculation subtracts it from the need: otherwise the Analyst would advise every
    day
    to order what is already on its way. `key` is the same as in the purchasing calculation:
    'bag:<id>' (bulk bag, quantity in g/ml/pcs) or 'var:<id>' (packaging, in pieces)."""
    OPEN, RECEIVED, CANCELLED = 'open', 'received', 'cancelled'
    STATUS_CHOICES = [(OPEN, 'Expected'), (RECEIVED, 'Arrived'), (CANCELLED, 'Cancelled')]

    key = models.CharField(max_length=40, db_index=True, verbose_name='Item (key)')
    label = models.CharField(max_length=300, verbose_name='Name')
    quantity = models.DecimalField(max_digits=12, decimal_places=3, verbose_name='Quantity')
    unit = models.CharField(max_length=10, default='pcs', verbose_name='Unit')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN, db_index=True)
    ordered_at = models.DateField(default=timezone.localdate, verbose_name='Ordered')
    expected_at = models.DateField(null=True, blank=True, verbose_name='Expected')
    stock_at_order = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True, verbose_name='Stock at the moment of ✓',
        help_text="The item's stock when the owner pressed ✓: when it rises significantly, the Analyst considers the order received")
    note = models.TextField(blank=True, default='', verbose_name='Note')
    finding = models.ForeignKey(
        Finding, null=True, blank=True, on_delete=models.SET_NULL, related_name='+', verbose_name='Finding')
    created_by = models.CharField(max_length=254, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Ordered purchase'
        verbose_name_plural = 'Ordered purchases'
        ordering = ['-ordered_at', '-pk']

    def __str__(self):
        return f'{self.label}: {self.quantity} {self.unit} ({self.get_status_display()})'


class StockSnapshot(models.Model):
    """Daily stock snapshot. Stock history cannot be reconstructed after the fact, and without it you
    cannot
    compute 'how many days it was out of stock' and the lost revenue.

    variant_id / bag_id are plain numbers, not FKs: the snapshot must survive product deletion.
    """
    date = models.DateField(db_index=True)
    variant_id = models.PositiveIntegerField()
    stock = models.IntegerField()
    bag_id = models.PositiveIntegerField(null=True, blank=True)
    bag_quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)

    class Meta:
        verbose_name = 'Stock snapshot'
        verbose_name_plural = 'Stock snapshots'
        constraints = [
            models.UniqueConstraint(fields=['date', 'variant_id'], name='analyst_snapshot_uniq'),
        ]


class OrderAttribution(models.Model):
    """Where an order came from: utm tags and referrer saved by the frontend on arrival.

    A separate table rather than Order fields — so as not to touch the orders migrations.
    """
    order_id = models.PositiveIntegerField(unique=True)
    utm_source = models.CharField(max_length=100, blank=True, default='')
    utm_medium = models.CharField(max_length=100, blank=True, default='')
    utm_campaign = models.CharField(max_length=150, blank=True, default='')
    utm_content = models.CharField(max_length=150, blank=True, default='')
    referrer = models.CharField(max_length=300, blank=True, default='')
    landing_path = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Order source'
        verbose_name_plural = 'Order sources'


class SearchLog(models.Model):
    """Search queries by day. zero_count — how many times a query returned no products:
    this is a direct list of what people look for but the catalog does not have."""
    date = models.DateField(db_index=True)
    query = models.CharField(max_length=120)
    count = models.PositiveIntegerField(default=0)
    zero_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'Search query'
        verbose_name_plural = 'Search queries'
        constraints = [
            models.UniqueConstraint(fields=['date', 'query'], name='analyst_searchlog_uniq'),
        ]


class ErrorEvent(models.Model):
    """A code error collected from logs (analyst/errorlog.py). One record per “fingerprint” — exception
    type +
    location in our code; repeats only increase the counter.

    What is stored is SANITIZED: only frames of our code with the line, no variables, headers or
    request body, and the
    exception text is truncated, without e-mails or phone numbers. All of this is untrusted data
    (the message may contain someone else's text),
    so the fixer agent treats it as data, not as instructions."""
    CODE, EXTERNAL, OTHER = 'code', 'external', 'other'
    KIND_CHOICES = [(CODE, 'Code logic error'), (EXTERNAL, 'External failure'), (OTHER, 'Other error')]

    OPEN, FIXING, FIXED, IGNORED = 'open', 'fixing', 'fixed', 'ignored'
    STATUS_CHOICES = [(OPEN, 'Open'), (FIXING, 'Being fixed'), (FIXED, 'Fixed'), (IGNORED, 'Ignored')]

    fingerprint = models.CharField(max_length=40, unique=True)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=CODE, db_index=True)
    service = models.CharField(max_length=20, default='web')
    exc_type = models.CharField(max_length=200)
    message = models.CharField(max_length=300, blank=True, default='')
    where_path = models.CharField(max_length=200, blank=True, default='', help_text='Path in the repository, e.g. backend/catalog/views.py')
    where_func = models.CharField(max_length=120, blank=True, default='')
    where_line = models.PositiveIntegerField(null=True, blank=True)
    request_path = models.CharField(max_length=200, blank=True, default='')
    traceback = models.TextField(blank=True, default='')
    count = models.PositiveIntegerField(default=1)
    recent = models.JSONField(default=list, blank=True, help_text='Moments of repeats (epoch, seconds), the last 48 h')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN, db_index=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = 'Code error'
        verbose_name_plural = 'Code errors'
        ordering = ['-last_seen']

    def __str__(self):
        return f'{self.exc_type} · {self.where_path}:{self.where_func} ×{self.count}'

    def count_since(self, moment) -> int:
        edge = moment.timestamp()
        return sum(1 for t in (self.recent or []) if t >= edge)


class CodeFix(models.Model):
    """Auto-fix of one code error: from the agent's draft to rollout and confirmation
    (analyst/codefix/pipeline.py).

    Live state of the pipeline; every state change is visible to the owner as a card `codefix:<id>`
    in the 'System health' section."""
    QUEUED, DRAFTING, VALIDATING, READY, DEPLOYING, DEPLOYED = 'queued', 'drafting', 'validating', 'ready', 'deploying', 'deployed'
    REVIEWING = 'reviewing'
    CONFIRMED, ROLLED_BACK, REVERTING, REVERTED, FAILED, REJECTED = 'confirmed', 'rolled_back', 'reverting', 'reverted', 'failed', 'rejected'
    STATUS_CHOICES = [
        (QUEUED, 'Queued'), (DRAFTING, 'The agent is writing the fix'), (VALIDATING, 'Sandbox check'), (REVIEWING, 'Review'),
        (READY, 'Ready to deploy'), (DEPLOYING, 'Being deployed'), (DEPLOYED, 'Deployed, watching'),
        (CONFIRMED, 'Confirmed'), (ROLLED_BACK, 'Rolled back'), (REVERTING, 'Being reverted'),
        (REVERTED, 'Reverted'), (FAILED, 'Failed'), (REJECTED, 'Rejected'),
    ]
    ACTIVE = (DRAFTING, VALIDATING, REVIEWING, DEPLOYING, REVERTING)
    DONE = (CONFIRMED, ROLLED_BACK, REVERTED, FAILED, REJECTED)

    event = models.ForeignKey(ErrorEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name='fixes')
    fingerprint = models.CharField(max_length=40, db_index=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=QUEUED, db_index=True)
    mode = models.CharField(max_length=10, default='propose')
    attempt = models.PositiveSmallIntegerField(default=0)
    patch_id = models.CharField(max_length=32, blank=True, default='')
    patch = models.TextField(blank=True, default='')
    summary = models.CharField(max_length=400, blank=True, default='')
    reason = models.TextField(blank=True, default='')
    review = models.TextField(blank=True, default='', help_text="The reviewer's remarks (empty = approved)")
    approved = models.BooleanField(default=False, help_text='The reviewer approved')
    owner_approved = models.BooleanField(default=False, help_text='The owner pressed "Apply" (the analyst service deploys it)')
    patch_mac = models.CharField(max_length=64, blank=True, default='', help_text='Patch seal (HMAC with the analyst key)')
    review_mac = models.CharField(max_length=64, blank=True, default='', help_text="Seal of the reviewer's verdict (HMAC)")
    phases = models.JSONField(default=dict, blank=True)
    history = models.JSONField(default=list, blank=True, help_text='Previous failed attempts: [{patch, failure}]')
    stats = models.JSONField(default=dict, blank=True)
    simulate_failure = models.BooleanField(default=False)
    drill = models.BooleanField(default=False)
    base_sha = models.CharField(max_length=40, blank=True, default='')
    commit = models.CharField(max_length=40, blank=True, default='')
    job_id = models.CharField(max_length=32, blank=True, default='')
    job_started_at = models.DateTimeField(null=True, blank=True)
    wait_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    deployed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Auto-fix'
        verbose_name_plural = 'Auto-fixes'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.fingerprint[:8]} · {self.get_status_display()}'
