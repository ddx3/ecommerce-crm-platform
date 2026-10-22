# The Analyst and shop-helper: operations

This document covers operating the Analyst and the helper; general deployment is described in DEPLOY.md.

## The Analyst (purchasing + server watchdog)

The `analyst` service (`manage.py run_analyst`) does two things and puts its findings in the 'Analyst' section
of the CRM inbox (`/admin/analyst/`, visible only to `ANALYST_OWNER_EMAIL`):

1. **Once a day** at `ANALYST_RUN_HOUR` (8:00, Europe/Kyiv): purchasing advice (below) and all
   system health checks (disk, backup, SSL, scheduler, helper, instruction queue).
2. **Every 5 minutes — the "watchdog"** (`analyst/guard.py`): cheap system health checks (no LLM) and,
   depending on `ANALYST_AUTOFIX`, autonomous server maintenance.

The Analyst does not touch orders, stock or prices (orders are handled by the manager; the stock code
calculates the remaining packagings itself, and `heal_stock_drift` in the scheduler heals drift). It writes only to the `analyst_*` tables
and, via `shop-helper`, to the server.

- The `analyst` migrations are needed before the service's first start (`web` applies them).
- The backups directory `./backups` (`/opt/shop/backups`) is mounted into the service read-only as
  `/backups` (`ANALYST_BACKUP_DIR`): it sees both manual dumps and the helper's auto-backups.
  If there are no dumps or the latest is older than 3 days, the Analyst reports it by itself.
- The helper queue `./ops-queue` is mounted into `web` and `analyst` as SUBDIRECTORIES (`requests/` — write, `artifacts/` — write only in
  `analyst`, `results/` — read-only): in `auto` mode the Analyst puts requests there itself, and `shop-helper` executes them only from the whitelist
  (disk, backup, scheduler restart).
- Scheduler heartbeat: `scheduler` writes `analyst_jobheartbeat`; until it exists
  (before the scheduler is updated), the Analyst will show 'The scheduler is not writing a heartbeat'.
- Manual run: `docker compose exec analyst python manage.py run_analyst --once --force`;
  show the findings without writing anything: `... run_analyst --once --dry-run`;
  one watchdog cycle: `... run_analyst --guard`.
- **Who fixes the server (`ANALYST_AUTOFIX`; in `docker-compose.yml` it defaults to `auto`; to change it, set
  `ANALYST_AUTOFIX=ask` in the root `.env` and run `docker compose up -d web analyst`):** the principle — the Analyst acts automatically
  ONLY on the technical side of the server; everything else (orders, stock, prices, purchasing) is
  information in cards only.
  - `auto` (prod) — the Analyst performs the permitted actions itself, no buttons. Every action leaves a card 'The Analyst did it itself: …'
    in the table (there is no separate log). Permitted ONLY: disk cleanup at ≥90% (without rollback images), a DB backup,
    a scheduler restart (when it is silent or a job is hung; NOT when a job simply fails). Safeguards:
    a pause of `ANALYST_AUTOFIX_COOLDOWN_HOURS` (6 h) between attempts, and the same action triggered by a different finding is not
    repeated during that pause; if the problem remains after the action, the Analyst does NOT repeat it but writes a card 'The Analyst failed to cope';
    no more than `ANALYST_AUTOFIX_MAX_PER_DAY` (3) identical actions per day; a finding the owner closed with ✓ is not touched;
    findings that automation cannot cure (no heartbeat after a release, the backups directory is not mounted, a job fails
    every time) are only shown. The helper also enforces its own limits (6 applies per hour, a 2-minute pause on the same action).
  - `ask` — buttons 'Free up space' / 'Back up now' / 'Restart the scheduler': first a plan with the exact
    list, then after 'Apply' — the action. This is the switch for MAINTENANCE automation; it does not turn off the auto-fixing of code errors
    (`ANALYST_CODEFIX`, section below).
  - `off` — cards only.
- **Purchasing.** The code (`analyst/purchasing.py`) calculates daily, for each of our own items: sales rate, seasonality
  from past years (Horoshop history 2024–2026 + live orders), stock (for bags — in g/ml, the demand
  of all packagings of the bag is summed), coverage in days, people waiting ("notify me" subscriptions and likes
  on out-of-stock items), and what is already ordered. From this the 'Order …' cards are built in
  the 'Analyst' section — these are reports, there are no buttons. **✓ on a card = "ordered"**: the purchase is recorded
  (`PlannedPurchase`, there is no separate admin section), and the calculation subtracts it from the need. On the Analyst
  page it stays as a grey row 'Ordered …' (key `ordered:<item>`, `data.processed`): it disappears by itself
  when the item's stock has grown by at least half of the ordered amount compared to the moment of ✓ (`advice.settle_received`,
  `PlannedPurchase.stock_at_order`), or on ↩ (which cancels the record). Nothing is sent to the supplier;
  records older than 90 days are ignored by the calculation.
  The explanations are written by an LLM agent (`ANALYST_AGENT_ENABLED=True` in `.env`; disabled by default): read-only
  tools, no personal data, quantity no higher than 1.5x the calculated need, a separate
  token budget (`ANALYST_LLM_TOKENS_PER_DAY`, 1.5 million). Without the agent, the rule-based cards work.
  **Skeptical mode:** by default the agent does NOT recommend buying. Before the agent, the code filters out noise: a purchase that yields
  less than `ANALYST_MIN_REVENUE` UAH of revenue over the horizon (1000), and items with sales in fewer than
  `ANALYST_MIN_SALES_MONTHS` months of the year (2) — that is a clearance of leftover stock, not demand; and also a
  supply batch (per the invoices) that covers years of sales. Exception — ≥2 people are waiting or the stock is negative.
  For each item the agent (`ANALYST_MODEL`, opus by default, with "thinking" `ANALYST_THINKING_TOKENS`=8000)
  looks at: demand regularity, seasonality, other packagings and similar products we already sell, the product description,
  the batch size from invoices (if known), and submits only what passed all the checks (up to 8 cards).
  Parameters: `ANALYST_LEAD_DAYS` (supply lead time, 14) and `ANALYST_COVER_DAYS` (stock cover after it, 45).
  Manual run with the agent: `docker compose exec analyst python manage.py run_analyst --once --force --agent`
  (consumes the Claude subscription: ~1.5 min, for verification only), without writing: `--once --dry-run --agent`.

## shop-helper: the on-server helper (actions for the Analyst's buttons)

The web container has no access to Docker or the host (and shouldn't: that would be root on the whole VPS),
so actions on the server itself are performed by a separate service on the HOST — `ops/shop_helper.py`
(Python stdlib, systemd). web only puts a request file into `ops-queue/` (`/ops` in the container).

What it can do (a whitelist, everything else is rejected): free up space (Docker build cache,
the system journal down to 200 MB, `btmp`, dangling images; rollback images — only by explicit choice,
the newest N releases are kept, images of running containers are not touched), make a backup
(`pg_dump` → `pg_restore -l` check → `backups/db_auto_<date>.dump`, auto-backups older than 14 days
are deleted, manual dumps never), restart `scheduler`/`advisor`/`analyst`.
**Every day after 03:00 it makes an auto-backup by itself** if there is no fresh one.
Protection: every action is first a "plan" (changes nothing), then "apply" only by the plan id
(the stored plan is executed, not what arrived in the request); a limit of 6 applies per hour.

Installation (as root on the VPS, FROM A FULL COPY of the project: `install.sh` takes the policy from `backend/analyst/`):
```bash
sh ops/install.sh                     # code → /opt/shop-helper, ops-queue/, shop-helper.service, start
systemctl status shop-helper          # alive?  cat /opt/shop/ops-queue/results/status.json — heartbeat
journalctl -u shop-helper -n 50       # what it did
```
Updating the helper's code = run `sh ops/install.sh` again (the script restarts the service itself).
The service can be installed BEFORE the Analyst release: it is independent of Django and immediately provides
the daily auto-backup (it makes the first dump at start if it is already after 03:00).

## Auto-fixing code errors

The Analyst notices an error in the code on its own, writes a fix, verifies it in an isolated sandbox, rolls it out to prod, and watches that
nothing got worse. Mode: `ANALYST_CODEFIX` = `off` | `propose` | `auto` (separate from `ANALYST_AUTOFIX`, which controls only
server maintenance: disk, backup, scheduler restart; `ANALYST_AUTOFIX=ask` does NOT turn off code rollouts).
To turn it off entirely: `ANALYST_CODEFIX=off` and `docker compose up -d --no-deps analyst web` (this stops only the pipeline; to
immediately stop a rollout that is already in progress too — the file `/opt/shop-autofix/PAUSE`, see "Manual deploy").

### The chain
1. **Collection.** The log handler (`analyst/errorlog.py`, wired in `config/settings.py`, including `uvicorn.error` for the WebSocket chat)
   collects errors with a traceback into `ErrorEvent`: a sanitized traceback (our code only, no variables or request bodies), an error "shape"
   without values (names, addresses, phone numbers are not stored), a fingerprint "type + class.method + file". Bots (`DisallowedHost`), 404s and
   client disconnects are not stored; network/disk/external-service failures are kind `external`, and everything that is not a sign of a logic
   error is `other`; only `code` (AttributeError, TypeError, KeyError, ValueError…) is cured by code. The card 'Code error: …'
   appears after 3 repeats per day; records older than 60 days are purged.
2. **Selection** (`codefix/pipeline.py`). An error is taken if it is in an ALLOWED and suitable file (not too large, no CR/NUL, with a
   trailing `\n`), repeats ≥3 times per day, no more often than once per 24 h and no more than 3 runs per day;
   the one that is hotter over the day goes first. The repeat count does NOT grant the right to roll out during the day (there are no urgent rollouts).
3. **Draft** (`codefix/drafter.py`). The agent (opus) works in a temporary copy of the code using only its own tools
   (read, search — in a separate process with a deadline, replace a fragment, add a NEW test); there is no Bash and no network. For it, the data from the
   error is untrusted. A subscription limit or an LLM failure is not an agent refusal: the step is postponed (`LLMUnavailable`),
   and the attempt is not counted.
4. **Sandbox** (the helper on the VPS, `ops/codefix_host.py`). Every phase gets a NEW temporary database (Postgres 18 with `--network none`,
   in memory) and a NEW application container in its network namespace (no internet and no host at all), the image is the same
   one that is running (by container id), WITHOUT env_file, read-only filesystem, `--cap-drop ALL`, limits on memory/processes/
   file size/output, `--oom-score-adj 1000` (the sandbox dies first). Four separate trees and processes: (a) base + new tests
   — must FAIL specifically via test methods (not via import or setup) and all of them must start; (b) fix + new tests — must
   PASS; (c) fix WITHOUT the new tests + the application's existing tests (for `core/*` — all dependent apps); (d) base
   for comparing old failures. The verdict comes from the return code and the runner's summary in stderr (the test's stdout decides nothing).
5. **Reviewer** (`codefix/reviewer.py`). A separate model call sees only the TYPE and LOCATION of the error (not its text), the description and the full diff
   (code before tests) and looks for side effects, in particular "hiding" the error (`except Exception: pass`, a silent default
   value, a logging change) and bypassing access checks. Approval requires TWO separate reviews in a row (the second with
   an emphasis on bypassing checks); even one rejection is a rejection. The verdict is STICKY and sealed: the first final one stands, and erasing the counters
   in the DB and moving the "review" step back does not give a new throw. Reviewer unavailable — retry after 15+ min, after three attempts there is no
   auto-deploy (the decision is manual). Not approved — the fix is NOT rolled out, neither automatically nor by a button (in that case there is no button).
6. **Seals and signature.** The key `/opt/shop-autofix/codefix.key` exists only on the helper and in the `analyst` container (it is not in `web`). The patch (together with
   the artifact name `patch_id` and the `drill`/`simulate_failure` flags) and the reviewer's verdict in the DB are sealed with
   HMAC (`patch_mac`, `review_mac`); analyst signs every task to the helper (`auth: {exp, mac}`) and puts into it the patch's sha256
   (`patch_sha`), which the helper checks against the artifact in the queue, and without a valid signature it does nothing. A compromised `web` can neither submit
   a patch, nor order a rollout, nor "approve" a record in the DB, nor substitute a different artifact. Whether to roll out at all is determined by the mode of
   the analyst service (`ANALYST_CODEFIX`), not by a field in the DB. The only thing web can forge is the `owner_approved` flag (the 'Apply'
   button goes through it): it allows rolling out, earlier than the night window, a fix that the reviewer has already approved, and it
   cannot override a reviewer's rejection. This is a residual risk, see below.
7. **Rollout** (only `auto`, or the owner's 'Apply'). The helper (under the lock `/opt/shop-autofix/deploy.lock`): check
   that the files in the running image match git → a fresh `pg_dump` → rollback tags `shop-<svc>:pre-autofix-<label>` from the images of
   the RUNNING containers → the `inflight.json` marker → `git apply` + a structure check of the real files + a local Analyst commit
   → **a thin layer on top of the running image** (only the changed files, no network and no build cache: `FROM shop-web:pre-autofix-…`
   + `COPY`) → `compose up -d --no-build --no-deps web scheduler analyst` → up to 3 min of checks (the site, containers running, no new LOGICAL
   tracebacks in the logs: we parse the blocks, bot noise and external-service failures are not counted). If it fails —
   an automatic rollback of code and images; if even after the rollback the site is not confirmed — `rollback_unhealthy`, a critical card, and the
   marker stays (the helper will retry at start). Downtime during the restart ≈ a minute (web comes up in 45–60 s).
8. **Monitoring.** For a day after the rollout the error must not recur (≥3 repeats → automatic cancellation by `git revert` + a layer).
   After a day — "confirmed".

### Policy: what the Analyst MAY change
Single source: `backend/analyst/codefix_policy.py` (a copy lives in `/opt/shop-helper/` outside git — the helper checks the patch with its own
copy; a mismatch is visible in `results/status.json.policy_sha`, and then the auto-fixes stand still). The principle — **only what is proven is allowed**, not
"the known is forbidden": a blacklist can be bypassed (both independent audits showed this), positive rules are much harder to bypass.

**Files and who decides.** Allowed are individual storefront modules (`catalog/api_views|serializers|views|…`, `blog`, `courses` API, `core/…`) and NEW
test files; never — payments and orders (`orders`), authorization (`users`), `config`, models, signals, migrations, stock, emails to customers,
keys, the Analyst itself, the Telegram bot, templates and frontend, Dockerfile/compose, `catalog/instruction_agent.py` (it assembles the LLM agent's permissions).
Two tiers: **helper modules** with no access and no writes (images, label printing, parsing, widgets, the canary) are rolled out on their own — by the night
window; **files where it is decided what is visible and to whom** (API, serializers, CRM views, feed, moderation, media, sitemap, embedding,
bundle instructions — `policy.OWNER_ONLY`) the Analyst prepares to the end (sandbox, two reviews) but ROLLS OUT only after the owner's 'Apply',
even in `auto` mode. Limits: ≤3 code files, ≤60 lines, ≥1 new test, existing tests must not be changed.

**Two layers.** Textual (forbidden words in ADDED lines after NFKC, invisible characters, the encoding line `# coding:`) and **structural
(AST)**, which compares the old and new code of the file:
- *Writing to foreign state* (R2): writing to an attribute or element is allowed only into a FRESH local object (a new collection, an instance, a row from a
  model query) and `self.x = …`; anything read from a parameter, attribute, call or container is foreign (this closes the "laundering" of a class
  through a helper function, dictionary, `self.attr`, loop). Mutating methods (`append/update/add/…`) — only on fresh local collections.
- *Calls* (R14): new calls only of builtins, methods of "pure" types/dates/read queries, functions of whitelisted modules, and
  whatever the file ALREADY calls; calling a VARIABLE (a parameter, a loop variable, a value from a dictionary or a call) is always rejected, except lambdas;
  at import time (module, class body, default values, decorators, annotations) — only the whitelist, without "the file already calls this".
  Field names in ORM queries are literals in the patch itself (R14b): `order_by(request.GET['sort'])`, `filter(**params)` are rejected.
- *Imports* (R4): from our own apps, only models and serializers are new; every imported name is checked.
- *Existing protection* (R7): you cannot change/remove/wrap an existing check with an early exit, a decorator, a function, a class base; a function
  that ITSELF checks access (words user/permission/owner/paid/…), or ITSELF writes to the DB, sends, accesses the network/files or calls
  such a function (transitively), or by its name is responsible for access/pagination/rate limiting, **is not auto-fixed at all**
  (R7e/R7f); you cannot add a new call of such a function anywhere (R7h), a new data source (R7j) or a second query from a manager where the
  selection is by visibility/owner (R7i), remove mentions of visibility or calls to a function with a filter (R7k), change constants
  that by name define visibility/permissions/paths (R7g), replace an existing name by rebinding (R7l).
- *Contracts*: in the serializers' `class Meta` you cannot add a field, remove `read_only_fields`, introduce `exclude`, change `depth`/
  `extra_kwargs`/`model` (R17); you cannot add unreachable code after `return/raise` (R18) and `while True` (R15).
Errors in functions the policy won't let be edited, the Analyst does not even pick up (it doesn't burn the subscription limit), and in the error card
it immediately writes 'a manual fix is needed'. Benchmark: about 60% of the functions in allowed files remain available for auto-fixing. The rules, their
boundaries and examples are in `tests_codefix_structure.py`, `tests_codefix_review.py`, `tests_codefix_review2.py`. To extend the policy = change
this file and run `sh ops/install.sh` on the VPS again.

**Residual risk (honestly).** The policy catches dangerous CONSTRUCTS, not intentions: a change that alters business logic in allowed
files and does not touch what is listed above (for example, changes a numeric limit, the text of a moderation prompt, the composition of a response dictionary, a path from a calculation)
passes the static checks, and what remains against it are the sandbox (tests), two independent reviewers, size limits, the night window, daily
monitoring and auto-rollback — and in the "human decides" files also your 'Apply'. A compromised `web` can plant any `ErrorEvent` in the DB
(and thereby influence the text the agent sees) and set `owner_approved`; the result — a fix that passed the policy, the sandbox and
both reviewers may be rolled out earlier than the night window (and even without your click for the "human decides" files). Otherwise nothing can be
rolled out from web.

### Helper safeguards
Signed tasks; safe reading of the queue (`ops/safeio.py`: no links and no FIFOs; containers see only the subdirectories
`requests/` (write), `artifacts/` (write, analyst only), `results/` (read-only)); the rollout night window 00:00–07:00 Kyiv
time (bypassed only by the owner's decision); ≤2 rollout attempts per day with a pause of ≥3 h — ALL attempts are counted, and a check is
valid for one; one pipeline at a time; a rollout only for a patch that passed verification here, on THE SAME commit, within the last
6 h; uncommitted changes on the server, a busy `deploy.lock` or the `PAUSE` file block everything; after a failure in the middle of a
rollout, at helper start an automatic rollback (`inflight.json`, only if the marker is fresh — up to 3 h — and the code on the server
is still the same), interrupted tasks are marked `interrupted`.

### Manual deploy (mandatory)
1. `touch /opt/shop-autofix/PAUSE` — the helper will not start new tasks and will stop the one in progress at a safe step.
2. Run the whole sequence under the shared lock: `flock /opt/shop-autofix/deploy.lock sh -c 'git pull --ff-only && …'`.
3. The deploy key on the VPS is read-only, so auto-fixes sit on the server as local "autofix …" commits and are NOT in GitHub
   (the card 'The server has N auto-fixes that are not in git yet' will remind you). BEFORE `git pull`/a deploy from the working machine:
   `git fetch ssh://root@<VPS>/opt/shop <branch>:refs/heads/vps-autofix` → `git merge --ff-only vps-autofix` (the commit hash stays
   the same) → `git push` → `git branch -D vps-autofix`. If the branch on GitHub has moved ahead: `git rebase`/`cherry-pick` the auto-commit
   (the hash will change) and on the VPS `git fetch && git reset --hard origin/<branch>` — WARNING: the tree will get ahead of the running image, so
   after that do a normal deploy (`docker compose build web`, tags, `up -d --no-deps`) per the release checklist.
4. `rm /opt/shop-autofix/PAUSE`.
Patches are stored in `/opt/shop-autofix/applied/` (rolled out) and `rejected/` (rolled back). To roll back a specific auto-fix manually:
`docker tag shop-web:pre-autofix-<label> shop-web:latest` (and likewise `shop-scheduler`, `shop-analyst`) → `git revert <commit>` →
`docker compose up -d --no-build --no-deps web scheduler analyst`. Do not do `systemctl stop shop-helper` as a "freeze":
that is what `PAUSE` is for.

### Verifying the mechanism (drill)
`python manage.py run_analyst --codefix-drill` — the real pipeline on the training "canary" `core/canary.py` (each of the three
functions has a deliberate bug; the drill takes the first one not yet fixed). The drill is the only case where the helper permits a rollout outside
the night window and the limits (the `drill` flag is accepted only for a patch that touches only the canary). `--simulate-failure` deliberately
fails the health check after the rollout to prove the auto-rollback. `python manage.py run_analyst --codefix` — one pipeline step
manually.

### Installation on the VPS
1. From a full copy of the project (`git pull`): `sh ops/install.sh` — installs `/opt/shop-helper/*` (atomically, after checking all
   input files), creates `/opt/shop-autofix/` and the key `codefix.key`, and the `shop-helper` service. If an auto-fix task is in progress,
   the restart is not performed without `--force`.
2. After changing the mount scheme in `docker-compose.yml` (queue subdirectories, the key in analyst): `docker compose up -d --no-deps web analyst`.
3. Verification: `cat /opt/shop/ops-queue/results/status.json` — `policy_sha`, `key_fp` (matches the Analyst's key), `key_ok`,
   `paused`, `recovery`. The Analyst itself will show a card if something doesn't match.
