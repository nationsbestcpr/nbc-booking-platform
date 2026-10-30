# Source of truth: what the booking engine stores, and what it doesn't

The booking engine talks to eight other platforms. For every piece of data
they hold, this page decides whether the engine's database keeps it, and in
what form. The schema in [`db/schema.sql`](../db/schema.sql) follows this page;
each table's comment carries the same tag.

## The rule

Each piece of data has exactly one system of record. The engine keeps
something in Postgres only when it falls into one of five kinds:

| Kind | Meaning | When it's justified |
|---|---|---|
| **Owned** | The engine is the system of record. | Seats, holds, registrations, waitlists, payment state, transfers, refunds, instructor portal data, CS actions. |
| **Mirror** | A copy of another platform's data, refreshed by sync or webhook, never edited here. | Only when the engine needs it **(a)** inside a database transaction or constraint, **(b)** on a hot path the source's rate limit can't serve, or **(c)** joined to engine data in a search. |
| **Reference** | Just the other platform's ID. | Whenever the engine must find, update or reconcile the object later. |
| **Snapshot** | Copied once at a moment, then frozen on purpose. | When the source may change but the engine must remember what was true at the time (price paid, details as booked, terms accepted, attribution at purchase). |
| **Ledger** | Bookkeeping about integrations (queues, webhook receipts, run logs, tokens). | To make delivery reliable, idempotent and visible. |

Anything that fits none of these is **not stored**: the engine fetches it
live from its owner when needed, or never needs it at all.

Every mirror must have a named sync path. A mirror nobody refreshes becomes a
second, wrong source of truth.

## Platform by platform

### Airtable (NBC Master Base) — owns locations, courses, prices, seat caps, schedule

| Data | Engine stores | Why | Sync path |
|---|---|---|---|
| Metros, sites (public address, time zone, lat/long, parking notes) | **Mirror**: `metros`, `sites` | Class pages and search must not call Airtable (5 req/s per base). Sessions need a foreign key. | Airtable sync, every few minutes plus "Sync now" |
| Access code, POC, emergency contact | **Mirror**, separate private tables: `site_private_details`, `session_private_details` | Class packet in the instructor portal. Kept apart so no public query can reach them. | Airtable sync |
| Room Max Student Count | **Mirror**: `rooms.max_student_count`, snapshotted on `sessions.room_max_student_count` | Caps co-instructor raises inside the seat transaction. | Airtable sync |
| Course Master (family, max seats, price, companion price, concurrent group key) | **Mirror**: `course_families`, `products`, `session_products.price_cents` | Seat pool and price must be enforced in the same transaction as the seat. | Airtable sync |
| Class Schedule (date, time, site, course, status) | **Mirror** plus engine-owned capacity and status: `sessions` | The engine holds the seats, so it needs the class locally. | Airtable sync; `session_airtable_records` maps rows to sessions |
| Instructor assigned / co-instructor (before the portal ships) | **Mirror**, display only: `sessions.airtable_lead_instructor`, `airtable_co_instructor` | CS console shows who teaches from launch. | Airtable sync |
| Instructor assignment (after the portal ships) | **Owned**: `assignments`, written back to Airtable | Ops approves awards in the engine. | Outbox → Airtable write-back |
| Enrollment counts for ops dashboards | Not stored separately; the engine computes them and **pushes** to Airtable Enrollment Snapshot | Engine owns the numbers. | Outbox → Airtable |
| Rental fees, Location Payments, POC portal, Scheduling Trackers, POC approvals, Enrollware Export, Monthly KPI | **Not stored** | The engine never uses them. The sync doesn't even request these fields. | — |
| Raw Airtable record contents | **Not stored** (hash only, in `airtable_records`) | One mirror is enough. A raw copy would duplicate the private fields outside their protected tables. | — |

### Stripe — owns cards, charges, refunds-as-money, coupons, invoices

| Data | Engine stores | Why |
|---|---|---|
| Card numbers, card details | **Never** | Stripe-hosted Checkout. Card data never reaches NBC servers. |
| Product and price IDs | **Reference**: `products.stripe_product_id`, `stripe_price_id`; per-class `session_products.stripe_price_id` | Create Checkout sessions. A Stripe price may only be used when its amount equals the displayed price (enforced by CHECK). Otherwise checkout sends `price_data`. Today checkout always sends `price_data` from the class's own price, so these stay empty. |
| Checkout session IDs | **Reference**: `checkout_sessions` | Match webhooks to holds; expire the session at 15 minutes (D2). |
| Payment intent and charge IDs, amounts | **Reference + owned amounts**: `payments` | Refunds, reconciliation, the console's student view. |
| Refund IDs and amounts | **Owned**: `refunds` | Refunds done directly in the Stripe Dashboard come in by webhook (`source = 'stripe_dashboard'`) so the engine never shows a stale balance. |
| Coupons and promotion codes (definitions) | **Not stored** | Jennifer manages them in Stripe. |
| Code used on an order, discount amount | **Snapshot**: `orders.promotion_code`, `discount_cents` | Revenue reporting and refund maths. |
| Invoices for corporate / PO bookings | **Reference**: `orders.stripe_invoice_id` | Status comes from Stripe webhooks. |
| Stripe Customer objects | **Reference only** if Stripe creates one | No customer profile is kept in Stripe on purpose. |

### GHL (HighLevel) — owns contact details, conversations, pipelines, workflows

| Data | Engine stores | Why |
|---|---|---|
| Contact ID | **Reference**: `people.ghl_contact_id` | One contact per student. |
| Contact name, email, phone | **Mirror**: `people` | Console search by name, email or phone has to join contacts to registrations. GHL's limit (100 requests per 10 s) can't serve every search and roster. Kept current by the ContactUpdate webhook and the nightly reconciliation. |
| Name and details as booked | **Snapshot / owned**: `registrations.attendee_*`, `orders.buyer_*`, `waitlist_entries` name fields | The name on the certificate and the order as placed. Edited only through the console, then pushed to GHL. Live contact details are always read from `people`. |
| Opportunity ID | **Reference**: `registrations.ghl_opportunity_id`, `waitlist_entries.ghl_opportunity_id` | One opportunity per enrollment. It follows the registration across transfers. |
| Opportunity stage | **Not a source**. The engine owns the registration status and pushes the stage. It keeps `ghl_stage_pushed` only to spot drift. | Golden rule: never drag GHL stages. A dragged stage (OpportunityStageUpdate) becomes an `integration_issues` row, not a status change. |
| Pipeline and stage IDs | **Reference**: `ghl_stage_map` | They differ between Sandbox 2 and live, so they are data, not code. |
| Custom field IDs | **Reference**: `app_settings.ghl_custom_field_ids` | Same reason. |
| Conversations (texts, emails) | **Not stored**. Fetched live for the console's student view. Only `people.ghl_last_message_at` is kept, for a "new message" marker. | GHL is the system of record. Storing bodies would duplicate private messages. |
| DND / opt-out state | **Not stored** | GHL sends every customer message, so GHL enforces DND. The engine's `notifications` table refuses customer messages that bypass GHL. |
| Workflows (confirmations, reminders, coursework, reviews) | **Not stored** | They run in GHL, triggered by stage and field changes the engine pushes. |
| Source / first-touch fields | **Snapshot** on the engine side (`order_attribution`, `people.first_order_id` as the write-once anchor); written to GHL once | So write-once logic doesn't need to read GHL. |
| OAuth tokens | **Ledger**, encrypted: `oauth_connections` | Each refresh returns a new refresh token, so it can't live in Replit Secrets. Used once the Marketplace app (webhooks) is built; until then contacts and opportunities are written with a Private Integration token kept in Secrets (`GHL_API_TOKEN`), which doesn't rotate. |

### Microsoft 365 (Entra ID) — owns staff identity

| Data | Engine stores | Why |
|---|---|---|
| User object ID, tenant ID | **Reference**: `user_identities` (provider `microsoft`) | Bind sign-ins to an account. |
| Email, display name | **Mirror**: `user_accounts` | Shown in menus and the action log. |
| Who may sign in at all | **Not stored** | Entra's "assignment required" decides. |
| What a person may do (roles, permissions, dashboards) | **Owned**: `roles`, `permissions`, `role_permissions`, `user_roles` | The plan: "inside the console, mark who is a manager". Instructors (emailed sign-in links) use the same tables. |
| Staff Microsoft passwords, MFA | **Never** | Entra handles them. |

**Customer and instructor accounts** are the engine's own (`user_accounts`). Their email + password sign-in stores only a salted scrypt hash (`user_identities.password_hash`), never the password. An account links to its GHL-mirrored contact (`user_accounts.person_id`) once the person books.

### GA4, Meta (pixel + Conversions API), Google Ads — own analytics and ad data

| Data | Engine stores | Why |
|---|---|---|
| UTMs, gclid/gbraid/wbraid, fbclid/fbp/fbc, landing page, referrer (first and last touch) | **Snapshot**: `order_attribution` | Needed for server-side conversions and GHL attribution. Frozen at purchase. |
| Shared conversion event ID | **Owned**: `orders.conversion_event_id` | Browser and server send the same ID, so each sale counts once. |
| GA4 client ID, IP, user agent | **Snapshot**, short-lived: `orders.ga_client_id`, `client_ip`, `client_user_agent` | Required by the Measurement Protocol / CAPI. IP and user agent are purged after the retention window. |
| Consent state at checkout | **Owned**: `orders.tracking_consent`, `consent_records` | Enhanced conversions only with consent. |
| Page views, funnel events, ad spend, reports | **Not stored** | They live in GA4 and the ad platforms. The Monthly KPI rollup reads them there. |
| Conversion send status | **Ledger**: `outbox_jobs` (targets `meta_capi`, `google_ads`, `ga4`) | Retries and proof of sending. |

### QuickBooks Online — owns bills and payments to instructors

| Data | Engine stores | Why |
|---|---|---|
| Pay record per instructor per class | **Owned until exported**: `pay_records` | Created when the class is marked Complete. |
| Vendor / bill references | **Reference**: `instructors.qbo_vendor_id`, `pay_records.qbo_reference` | Reconcile what was exported. |
| Bills, payments, accounting | **Not stored** | |

### Enrollware — owns registrations sold before each metro's cutover

| Data | Engine stores | Why |
|---|---|---|
| Seats sold in Enrollware for classes after cutover | **Snapshot**, counts only: `external_seat_reservations` | Otherwise the engine would sell those seats again. |
| Enrollware rosters and students | **Not stored** | They stay in Enrollware until class day. |
| Which system a metro uses | **Owned**: `metros.booking_system` | Never both. Enforced: the engine refuses holds in Enrollware metros. |

### American Red Cross (RCLC) — owns certificates

| Data | Engine stores | Why |
|---|---|---|
| Roster export (RCLC-ready) | Generated on demand. `sessions.roster_exported_at` plus an audit row. | |
| Certificates, online coursework completion | **Not stored** | They live in RCLC. Only Instructor Training prerequisites received are noted (`registrations.prerequisites_done_at`), because the policy's 3-day deadline depends on it. |

### Zapier

Side jobs only. Nothing in the core flow reads from or writes to Zapier, and
nothing here is stored for it.

## Things the engine owns that no other platform has

- Seat inventory: holds, capacity, overrides and their reasons
- Registrations and their full lifecycle, transfers, cancellations and forfeits
- Waitlist entries, offers and their timing
- Accounts, roles, permissions and which dashboard each role opens
- Instructor eligibility, requests, awards, assignments, attendance marks and pay records (from the portal launch)
- CS notes, the audit log, consent evidence, accepted terms versions
- Public page copy approved by Bre (`page_content`) and URL redirects

## Environments

Staging and production are separate databases, never one database with a mode
flag:

- **Stripe:** test vs live.
- **GHL:** Sandbox 2 vs live sub-account.
- **Airtable:** there is only one base, the NBC Master Base. Staging may read
  it but must not write to it. `app_settings.airtable_writeback_enabled` is
  `false` everywhere except production.

`orders.livemode` and `payments.livemode` guard against a test event reaching
the live database.
