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

## Which way data flows (decided 11/20)

| Between the booking engine and | Direction | How |
|---|---|---|
| **NBC Ops Hub** | **Both ways** | The Ops Hub reads classes, students, bookings, refunds and waiting lists, and asks for every desk action (refunds and their approval, moves, cancellations, phone and invoice bookings, attendance, waiting-list changes) through the engine's API, `/api/v1` ([docs/api.md](api.md)). Each call names the staff member it is for; the engine checks their roles and cities and keeps the record. The Ops Hub keeps no copy of booking data. |
| **GHL** | **One way: engine → GHL** | GHL is only for messaging. The engine sends what emails and texts need (contact, opportunity, stage, class details, tags) and never reads data back; the only reads are GHL's own contact and opportunity ids, so nothing is made twice. **No instructor data goes to GHL** (the opportunity's old "Instructor" field is no longer filled). GHL webhooks (`/webhooks/ghl`) stay unbuilt. |
| **Airtable** | **Read only: Airtable → engine** | The schedule, locations, courses, prices and instructors come in by the Airtable sync. Nothing is written back for now: the write-back rows below stay off (`airtable_writeback_enabled` is false). |
| **Stripe** | Both ways (money) | Checkout, refunds and invoices go out; payment webhooks come back. |

The booking app itself keeps only user management (the Superadmin), the
instructor portal and the student portal (with booking and checkout). The old
desk and admin pages are a Superadmin-only fallback until the Ops Hub's
screens are live (`app/areas.py`).

## Platform by platform

### Airtable (NBC Master Base) — owns locations, courses, prices, seat caps, schedule

| Data | Engine stores | Why | Sync path |
|---|---|---|---|
| Metros, sites (public address, time zone, lat/long, parking notes) | **Mirror**: `metros`, `sites` | Class pages and search must not call Airtable (5 req/s per base). Sessions need a foreign key. | Airtable sync, every few minutes plus "Sync now" |
| Access code, POC, emergency contact | **Mirror**, separate private tables: `site_private_details`, `session_private_details` | Class packet in the instructor portal. Kept apart so no public query can reach them. | Airtable sync |
| Room Max Student Count | **Mirror**: `rooms.max_student_count`, snapshotted on `sessions.room_max_student_count` | Caps co-instructor raises inside the seat transaction. | Airtable sync |
| Course Master (family, max seats, price, companion price, concurrent group key) | **Mirror**: `course_families`, `products`, `session_products.price_cents` | Seat pool and price must be enforced in the same transaction as the seat. | Airtable sync |
| Class Schedule (date, time, site, course, status) | **Mirror** plus engine-owned capacity and status: `sessions` | The engine holds the seats, so it needs the class locally. | Airtable sync; `session_airtable_records` maps rows to sessions |
| Test classes on the test site only (`sessions.slug` starting `nbc-test-`) | **Engine-made**, no Airtable row | Admin → Test data loads ready-made classes and bookings to try the desk on; the sync only touches classes mapped in `session_airtable_records`, so it leaves them alone. Refused on the live site and on a database labelled production. | Admin → Test data, or `flask --app wsgi testdata load` |
| Instructors table (0011): name, email, phone, address, city areas, r.25 certification expiry dates, additional certs, availability (days, times, classes per month, travel, notice, notes), outreach, removal, HUB team, page type, pre-approved and taught-at locations, other Enrollware accounts | **Mirror**: `instructors`, `instructor_certs`, `instructor_metros`, `instructor_hub_members`, `instructor_locations`; Cert Status is the `instructor_cert_status` view | Airtable owns instructor data (11/16). The Ops Hub reads it through `/api/v1/instructors` instead of Enrollware. The GHL fields on the table are not read (11/20: no instructor data in GHL). An instructor with no email, or one another instructor already has, is kept without it. | Airtable sync (full syncs) |
| Master Locations' instructor fields (0011): assignment type, primary / HUB primary / HUB secondary instructor, instructor training location, instructor notes, extra pay per class and reason | **Mirror**: `location_instructor_settings`, for every location (staff only: it holds pay) | Who staffs each location; extra pay reaches only people with pay.view_all. | Airtable sync |
| Class Schedule's instructor fields (0011): ASSIGN INSTRUCTOR matched to an instructor, Instructor Assigned, availability status, performance tier | **Mirror**: `session_instructor_details` | The lead per class for the Ops Hub and the instructor portal. A name that matches no instructor stays as text and is listed once. | Airtable sync |
| Instructor Recruiting Needs (0011) | **Mirror**: `recruiting_needs`, `recruiting_need_classes` | Recruiting work moves off Enrollware with the rest. | Airtable sync (full syncs) |
| Instructor assigned / co-instructor (before the portal ships) | **Mirror**, display only: `sessions.airtable_lead_instructor`, `airtable_co_instructor` | CS console shows who teaches from launch. | Airtable sync |
| Instructor assignment (after the portal ships) | **Owned**: `assignments`. Written back to Airtable only once write-back is turned on (it's off: Airtable is read-only for now) | Ops approves awards in the engine. | Outbox → Airtable write-back (off) |
| Enrollment counts for ops dashboards | Not stored separately; the engine computes them. The Ops Hub reads them through `/api/v1/classes`; pushing them to Airtable's Enrollment Snapshot waits for write-back (off) | Engine owns the numbers. | API; Outbox → Airtable (off) |
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

### GHL (HighLevel) — messaging only: conversations, pipelines, workflows. The engine sends; it never reads data back

| Data | Engine stores | Why |
|---|---|---|
| Contact ID | **Reference**: `people.ghl_contact_id` | One contact per student. |
| Contact name, email, phone | **Owned**: `people` (decided 11/16: the booking engine's database is the source of student data) | Sent to GHL for emails, texts and campaigns, never read back from it (11/20: one way). Changes are made here (or through the Ops Hub's API calls) and pushed to GHL. |
| Name and details as booked | **Snapshot / owned**: `registrations.attendee_*`, `orders.buyer_*`, `waitlist_entries` name fields | The name on the certificate and the order as placed. Edited only through the console, then pushed to GHL. Live contact details are always read from `people`. |
| Opportunity ID | **Reference**: `registrations.ghl_opportunity_id`, `waitlist_entries.ghl_opportunity_id` | One opportunity per enrollment. It follows the registration across transfers. |
| Opportunity stage | **Not a source**. The engine owns the registration status and pushes the stage. It keeps `ghl_stage_pushed` only to spot drift. | Golden rule: never drag GHL stages. The engine doesn't read stages back (one way), so a dragged stage is simply overwritten on the next change. |
| Pipeline and stage IDs | **Reference**: `ghl_stage_map` | They differ between Sandbox 2 and live, so they are data, not code. |
| Custom field IDs | **Reference**: `app_settings.ghl_custom_field_ids` | Same reason. |
| Conversations (texts, emails) | **Not stored**. Fetched live for the console's student view. Only `people.ghl_last_message_at` is kept, for a "new message" marker. | GHL is the system of record. Storing bodies would duplicate private messages. |
| DND / opt-out state | **Not stored** | GHL sends every customer message, so GHL enforces DND. The engine's `notifications` table refuses customer messages that bypass GHL. |
| Workflows (confirmations, reminders, coursework, reviews) | **Not stored** | They run in GHL, triggered by stage and field changes the engine pushes. |
| Source / first-touch fields | **Snapshot** on the engine side (`order_attribution`, `people.first_order_id` as the write-once anchor); written to GHL once | So write-once logic doesn't need to read GHL. |
| Tags the engine adds (who booked, course, city, add-ons, bring a friend, send invite) | **Owned names**: `app_settings.ghl_tags`. The tags themselves live on the GHL contact; the engine never reads them back. | GHL workflows (the emails built from the NBC sub-account's templates) start on tags, so the names are a setting staff can change to match a workflow. |
| Who booked a seat (booked by, their email, booking type), its add-ons, mapped question answers | **Not stored as a copy**. Pushed to opportunity custom fields from the engine's own `orders`, `order_addons` and `booking_answers`. | The engine owns the booking; GHL gets what its workflows and reports need. |
| Attendee invite link (`contact.nbc_invite_link`) and booker's name (`contact.nbc_booked_by`) | **Not stored**. Written to the contact when an invite goes out; only a hash of the link's token is kept (`action_tokens`, purpose `account_invite`), and each send is a `notifications` row. | GHL sends the invitation email from its template; the engine checks the link when it's opened. |
| OAuth tokens | **Ledger**, encrypted: `oauth_connections` | Each refresh returns a new refresh token, so it can't live in Replit Secrets. Used once the Marketplace app (webhooks) is built; until then contacts and opportunities are written with a Private Integration token kept in Secrets (`GHL_API_TOKEN`), which doesn't rotate. |

### NBC Ops Hub — owns its own desk work (CS log, QC, KPIs, team tools); reads and changes bookings through the API

| Data | Engine stores | Why |
|---|---|---|
| API keys the Ops Hub calls with | **Owned**: `api_keys` (a hash of the secret only), `api_key_permissions` | A Superadmin makes one key per tool (Superadmin → API keys). The key limits what the tool may do; the staff member named on each call (`X-Acting-User`) limits it further. |
| Who did what through the Ops Hub | **Owned**: `audit_log`, under the staff member's account, marked `via_api` with the key's name | The same action log as the booking app's own actions. |
| The Ops Hub's copy of classes, registrations and payments (the NBC Collector's Enrollware pulls) | **Not stored by the Ops Hub any more** (decided 11/16) | The Ops Hub calls `/api/v1` instead. |

### Microsoft (Entra ID) and Google — own their sign-in identities

| Data | Engine stores | Why |
|---|---|---|
| Microsoft object ID and tenant ID; Google account ID (`sub`) | **Reference**: `user_identities` (providers `microsoft`, `google`) | Bind sign-ins to an account. Accounts are matched by this id, not by email. |
| Email, display name | **Snapshot** at first sign-in: `user_accounts` (with `email_verified_at` when the provider vouches for the email) | Shown in menus and the action log; a confirmed email lets the account see seats booked for it by others. |
| Tokens (ID, access, refresh) | **Never** | Only the ID token's claims are read, once, at sign-in. |
| Who may sign in at all | **Not stored** | Anyone may sign in; signing in grants no role. Staff roles are granted by an admin. |
| What a person may do (roles, permissions, dashboards) | **Owned**: `roles`, `permissions`, `role_permissions`, `user_roles` | The plan: "inside the console, mark who is a manager". Instructors (emailed sign-in links) use the same tables. |
| Staff Microsoft passwords, MFA | **Never** | Entra handles them. |

**Customer and instructor accounts** are the engine's own (`user_accounts`). Online orders point at the account that booked them (`orders.buyer_user_id`, owned). Their email + password sign-in stores only a salted scrypt hash (`user_identities.password_hash`), never the password. An account links to its GHL-mirrored contact (`user_accounts.person_id`) once the person books.

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
- Booking questions and their answers (`booking_questions`, `booking_answers`), and add-ons with what each order bought (`addons`, `order_addons`): set up by staff in Admin → Booking setup, so a new question or add-on is data, not a release
- Who booked whom: `orders.buyer_*` / `buyer_user_id` against each `registrations.attendee_*`, friends' seats (`registrations.is_bring_a_friend`) and follow-up purchases (`orders.parent_order_id`), and attendee invites (`action_tokens`, `notifications`)
- The same-course rule (one email, one seat in a course per `same_course_block_months`), enforced by a trigger on `registrations`

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
