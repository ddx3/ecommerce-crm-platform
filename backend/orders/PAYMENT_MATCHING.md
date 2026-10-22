# Auto-confirmation of "Card transfer" (bank transfer to a card)

The system pulls the Mono Personal API statement and automatically confirms
orders with `payment_method='bank_transfer'` in three scenarios:

1. **Order number in the comment + exact amount** → AUTO
2. **Counterparty full name ≈ customer full name + exact amount** → AUTO
3. **Receipt screenshot from the customer matches a real transaction** → AUTO (anti-fraud)

In all other cases the transaction goes to the manual confirmation queue.

## Architecture

```
┌─────────────────────┐     ┌──────────────────┐
│  Mono Personal API  │────▶│  webhook (HTTPS) │──┐
│ (online push)       │     │  /api/webhooks/  │  │
└─────────────────────┘     │   monobank-      │  │
                             │   personal/<sec> │  │
                             └──────────────────┘  │
                                                    ▼
┌─────────────────────┐     ┌──────────────────┐  ┌──────────────────┐
│  Mono Personal API  │────▶│  poll_monobank   │─▶│ BankTransaction  │
│ (fallback poller)   │     │  cron 5 min      │  │ (DB)             │
└─────────────────────┘     └──────────────────┘  └────────┬─────────┘
                                                            │
                                                            ▼
┌─────────────────────┐     ┌──────────────────┐  ┌──────────────────┐
│ Customer uploads    │────▶│ screenshot_match │  │  bank_matcher    │
│ payment_screenshot  │     │  (Gemini Vision) │  │  (3 factors)     │
└─────────────────────┘     └────────┬─────────┘  └────────┬─────────┘
                                     │                     │
                                     └──────────┬──────────┘
                                                ▼
                                       ┌──────────────────┐
                                       │   PaymentMatch   │
                                       │  Order.paid      │
                                       └──────────────────┘
```

## .env configuration

```bash
# Mono Personal API (statements)
MONOBANK_PERSONAL_TOKEN=u-...                       # api.monobank.ua → "Create token"
MONOBANK_PERSONAL_WEBHOOK_SECRET=Y-fEW...           # python -c "import secrets; print(secrets.token_urlsafe(32))"

# Privat24 for business (FOP statements). No webhooks, polling only.
PRIVAT_BUSINESS_TOKEN=...                           # business.privatbank.ua → "Corporate API"
PRIVAT_BUSINESS_CLIENT_ID=                          # optional, if the API requires it

# Gemini Vision (OCR for screenshots)
GEMINI_API_KEY=AIza...                              # Already set

# Basics
BACKEND_URL=https://your-domain.com                  # HTTPS is REQUIRED in production
```

## First run (one-time, in production)

```bash
# 1. DB migrations
python manage.py migrate

# 2. List the Mono accounts
python manage.py mono_list_accounts

# 3. Register the FOP account in the DB (id from step 2)
python manage.py mono_register_account E-XXXXXXXXXXXXXXXXXXXX --name "Mono FOP"

# 4. Backfill history for the last 7 days (optional — for verification)
python manage.py poll_monobank --hours 168

# 5. Register the webhook in Mono (requires public HTTPS)
python manage.py mono_set_webhook
# Or manually:
python manage.py mono_set_webhook --url https://your-domain.com/api/webhooks/monobank-personal/<secret>/

# 6. Verify the webhook is set
python manage.py mono_set_webhook --check

# 7. Privat24 for business (in parallel)
python manage.py privat_register_account UA903052990000026000011710000 \
    --name "Privat FOP"
python manage.py poll_privat --hours 168
```

## Cron jobs in production

If the webhook works (public HTTPS), the poller is needed only as a **backup**.
If there is no HTTPS, the poller is mandatory.

### Linux (cron)

```cron
# Every 5 minutes — catch up on missed webhooks and acquiring
*/5 * * * * cd /var/app && /var/app/venv/bin/python manage.py reconcile_pending_payments

# Every minute — Mono statement poller (backup for the webhook)
* * * * * cd /var/app && /var/app/venv/bin/python manage.py poll_monobank

# Every 2 minutes — matcher (processes new unmatched transactions)
*/2 * * * * cd /var/app && /var/app/venv/bin/python manage.py match_bank_transactions

# Every 5 minutes — OCR of screenshots uploaded by customers
*/5 * * * * cd /var/app && /var/app/venv/bin/python manage.py process_payment_screenshots

# Privat: there are no webhooks, so the poller is mandatory (every 1-3 min)
*/2 * * * * cd /var/app && /var/app/venv/bin/python manage.py poll_privat
```

### Windows Task Scheduler (local machine)

`schtasks /create /tn "ShopMonoPoll" /tr "...python.exe manage.py poll_monobank" /sc minute /mo 1`

## Commands

| Command | Purpose |
|---------|---------|
| `mono_list_accounts` | List the Mono accounts from the token + a mark for the registered ones |
| `mono_register_account <id> --name "..."` | Add a Mono account to monitoring |
| `mono_set_webhook [--url ... \| --check \| --clear]` | Manage the Mono webhook |
| `poll_monobank [--hours N] [--account ID] [--dry-run]` | Pull the Mono statement (fallback) |
| `privat_register_account <IBAN> --name "..."` | Add a Privat IBAN to monitoring |
| `poll_privat [--hours N] [--account IBAN] [--interim] [--dry-run]` | Pull the Privat statement (mandatory cron) |
| `match_bank_transactions [--limit N] [--dry-run]` | Match unmatched transactions to orders (both banks) |
| `process_payment_screenshots [--order N] [--hours N]` | Screenshot OCR + auto-match |
| `reconcile_pending_payments` | Catch-up cron for acquiring (already existed) |

## How to verify (live test)

1. Create an order on the test frontend with the payment method **"Card transfer"** (bank transfer to a card)
2. Transfer the stated amount to the FOP's Mono account
3. **If the webhook is configured** — the order becomes `paid` within 1-3 seconds
4. **If not** — wait up to 1 minute for the cron, or run manually: `python manage.py poll_monobank && python manage.py match_bank_transactions`

## Logs / admin

- `/admin/orders/banktransaction/` — all statement transactions + status (matched/unmatched/manual_review/ignored)
- `/admin/orders/paymentmatch/` — Order ↔ Transaction pairs with score and reasons
- `/admin/orders/bankaccount/` — account details + time of the last sync

## Anti-fraud for screenshots

1. The customer uploads a file via POST `/api/orders/<num>/screenshot`
2. The view calls `match_screenshot(order)` synchronously
3. Gemini Vision (`gemini-2.5-flash`) returns JSON: amount, date, time, sender full name, card
4. Check: does the amount on the screenshot equal `order.total_price`?
5. Look for a `BankTransaction` with the same amount in a ±2 h window
6. **0 transactions** → possible fraud (Photoshop or the payment hasn't arrived yet) → manual
7. **1 transaction** → AUTO + paid
8. **>1 transactions** → goes to manual review

## Tests

```bash
python manage.py test orders.tests
```

Covers:
- The 3 matching factors (number+amount, full name+amount, noise filter)
- Time window for candidates
- Idempotency of a repeated run
- Screenshot match: 0/1/many transactions, amount mismatch, already paid

## Security

- The Mono Personal token gives **read-only** access to statements (operations are not possible)
- The Privat token must be issued **only for "Statements"** — do NOT grant access to operations
- The Mono webhook URL contains a 32-character secret (without it — 403)
- If the Mono token is compromised — revoke it at `api.monobank.ua` and generate a new one
- If the Privat token is compromised — revoke it at `business.privatbank.ua` → "Corporate API"
- If the webhook secret is compromised — generate a new one and run `mono_set_webhook` again

## Mono vs Privat differences

| Aspect | Mono Personal | Privat24 for business |
|--------|---------------|----------------------|
| Webhook (real-time) | ✅ | ❌ polling only |
| Delay before a transaction appears | 1-3 sec (webhook) | 30-90 sec (polling cron) |
| Account discovery via API | ✅ `client-info` | ❌ IBAN must be given explicitly |
| Auth header | `X-Token: ...` | `token: ...` |
| Transaction ID | `id` (base64) | `ID` (string) |
| Amount | kopecks (int) | hryvnias (string with a dot) |
| Sign | `amount` itself (− for debits) | separate `TRANTYPE` field (C/D) |
| Payment purpose | `comment` | `OSND` |
| Counterparty full name | Parsed from `description` "From: ..." | Ready-made in `AUT_CNTR_NAM` |
| Rate limit | 1 req/60s | ~1 req/60s |

**Conclusion:** the matching code is the same for both (fields are normalized in `BankTransaction`).
The difference is only at the client level (`monobank_personal.py` vs `privatbank_business.py`).
