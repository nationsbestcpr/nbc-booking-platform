# The booking engine's API (`/api/v1`): access list

The NBC Ops Hub does the desk's work by calling the booking engine. The
booking app itself keeps only user management (the Superadmin), the
instructor portal and the student portal (with booking and checkout).

| Platform | Direction |
|---|---|
| NBC Ops Hub | Both ways, through this API. The Ops Hub keeps no copy of booking data. |
| GHL | One way, engine → GHL, for messaging only. No instructor data. |
| Airtable | Read only, Airtable → engine (the sync). Nothing is written back for now. |

The same list is on **Superadmin → API keys** (read from the code, so it is
always current), and `GET /api/v1` answers without a key.

## Getting access

1. A Superadmin opens **Superadmin → API keys**, names the tool ("NBC Ops Hub"),
   ticks what it may do (the desk's permissions are ticked already) and makes
   the key. The key is shown **once**: put it in the Ops Hub's Replit Secrets
   as `BOOKING_ENGINE_API_KEY`. Never paste it in chat, email, Teams or a
   document. Only a hash is stored; **Switch off** stops it at once.
2. Every staff member the Ops Hub acts for needs an account in the booking
   engine with the right role: **Superadmin → Add a person** (they sign in
   with Microsoft or Google using that email, if they ever sign in here at
   all).

## Every call

```
Authorization: Bearer nbc_<prefix>_<secret>
X-Acting-User: jane@nationsbestcpr.com
Content-Type: application/json            (POST)
```

- The call runs **as the staff member** in `X-Acting-User`: their roles and
  cities, limited to what the key may do. A customer-service person can ask
  for a large refund but only a manager can approve it, exactly as in the
  database rules.
- Every change goes in the action log under that person, marked
  `via_api: "<key name>"`.
- Bodies and answers are JSON. Money is **integer cents**. Times are ISO 8601
  with their offset; show class times in the class's `time_zone`.
- Answers: `{"data": …}` (plus counts on lists); errors:
  `{"error": {"status": 400, "message": "…"}}` with the reason in plain
  English, ready to show staff.
- Status codes: `200` OK, `201` made, `400` refused (with the reason), `401`
  no or bad key, `403` the key or the person isn't allowed, `404` not there or
  in a city the person doesn't cover.
- Ids: classes and seats use their public ids (UUIDs). Refunds and
  waiting-list entries use numbers.

## Access list

"Needs" is any one of the permissions listed. Both the key and the person
must have it.

### About the key

| Method | Path | Needs | What it does |
|---|---|---|---|
| GET | `/api/v1` | (no key) | What this API is |
| GET | `/api/v1/me` | (any key) | The key and the staff member, with what they may do |

### Classes

| Method | Path | Needs | What it does |
|---|---|---|---|
| GET | `/api/v1/classes` | class.view | Classes in the person's cities with live seats. Filters: `city`, `course`, `status`, `from`, `to` (YYYY-MM-DD; from defaults to today), `full=yes/no`, `limit`, `offset` |
| GET | `/api/v1/classes/<class>` | class.view or student.view | One class: students with attendance, its waiting list, what may be done |
| GET | `/api/v1/classes/<class>/class-list.csv` | class.roster_export | The class list for the Red Cross (CSV) |
| POST | `/api/v1/classes/<class>/attendance` | booking.attendance | `{"seat": "<seat>", "mark": "checked_in" \| "complete" \| "no_show" \| "cleared"}` |
| POST | `/api/v1/classes/<class>/finish` | booking.attendance | Finish the class: grades final, instructor pay records made |
| POST | `/api/v1/classes/<class>/move-all` | booking.transfer_bulk | A cancelled class's students to another class: `{"to_class": "<class>", "note": "…"}` |
| POST | `/api/v1/classes/<class>/bookings` | booking.create_phone or booking.create_invoice | Book by phone (payment link by text and email) or on an invoice (below) |

### Students and bookings

| Method | Path | Needs | What it does |
|---|---|---|---|
| GET | `/api/v1/students` | student.view | Find a student: classes (soonest first) with matching seats. `q` (name, email, phone, booking number), `city`, `course`, `when` (upcoming, today, 7, 30, past30, any), `status`, `channel`, `from`, `to`, `page` |
| GET | `/api/v1/bookings/<seat>` | student.view | One seat with everything around it: order, payments and refunds (with payment.view), moves, notes, history, open payment link or invoice, and `can` (what may be done) |
| POST | `/api/v1/bookings/<seat>/notes` | student.note | `{"body": "…"}` |
| GET | `/api/v1/bookings/<seat>/move-options` | booking.transfer | Classes it can move to (same course), with prices and seats left. `q` narrows by place |
| POST | `/api/v1/bookings/<seat>/move` | booking.transfer | `{"to_class", "product_id", "reason", "fee_cents", "note", "extra_seat_reason"}`. A full class needs class.capacity_override and a reason |
| POST | `/api/v1/bookings/<seat>/cancel` | booking.cancel | `{"reason", "refund_cents", "note"}`. The seat goes back on sale (waiting list first) |
| POST | `/api/v1/bookings/<seat>/payments` | booking.create_invoice | An invoice paid outside Stripe: `{"amount_cents", "reference"}` |
| POST | `/api/v1/bookings/<seat>/payment-link` | booking.create_phone or booking.transfer | Send the open payment link again (GHL texts and emails it) |

### Refunds

| Method | Path | Needs | What it does |
|---|---|---|---|
| GET | `/api/v1/bookings/<seat>/refund-preview` | refund.issue | What can still be refunded, the reasons, the manager threshold |
| POST | `/api/v1/bookings/<seat>/refunds` | refund.issue | `{"amount_cents", "reason", "note"}`. Over the threshold it waits for a manager (`status: "requested"`), otherwise it goes to Stripe |
| GET | `/api/v1/refunds` | refund.issue or refund.approve_large | `status` = waiting, sending, done, problems or all; `q`. Includes `counts` for every status |
| POST | `/api/v1/refunds/<id>/approve` | refund.approve_large | A manager for that city approves; it goes to Stripe |
| POST | `/api/v1/refunds/<id>/turn-down` | refund.approve_large | `{"why": "…"}` |

### Waiting lists

| Method | Path | Needs | What it does |
|---|---|---|---|
| GET | `/api/v1/waitlists` | waitlist.view | Waiting lists of upcoming classes. `city`, `status`, `class`. Contact details only with student.view |
| POST | `/api/v1/waitlist/<id>/remove` | waitlist.manage | `{"why": "…"}` |
| POST | `/api/v1/waitlist/<id>/reoffer` | waitlist.manage | Offer a seat again after an offer expired |

### Reference

| Method | Path | Needs | What it does |
|---|---|---|---|
| GET | `/api/v1/instructors` | instructor.view | Instructors as the engine knows them (from Airtable): status, next certification expiry, upcoming classes |
| GET | `/api/v1/locations` | class.view | Cities and their locations |
| GET | `/api/v1/log` | audit.view | The action log: `since` (ISO; default 7 days), `area` (refund, booking, user …) |

Not in the API yet (fallback pages for the Superadmin): booking questions,
add-ons, business settings, integrations and test data.

## Examples

### A refund that needs a manager (Python, from the Ops Hub)

```python
import os, requests

BASE = "https://test-nbc-booking.replit.app/api/v1"
KEY = os.environ["BOOKING_ENGINE_API_KEY"]          # Replit Secrets, never in code

def call(method, path, acting_for, **json):
    res = requests.request(method, BASE + path, json=json or None, timeout=20, headers={
        "Authorization": f"Bearer {KEY}", "X-Acting-User": acting_for})
    body = res.json()
    if res.status_code >= 400:
        raise RuntimeError(body["error"]["message"])  # plain English: show it to staff
    return body["data"]

# Customer service asks for $108 back.
refund = call("POST", f"/bookings/{seat_id}/refunds", "cs.person@nationsbestcpr.com",
              amount_cents=10800, reason="customer_cancel", note="Called 11/20")
# refund["status"] == "requested" when it needs a manager

# The manager approves it from the Ops Hub's refund queue.
waiting = call("GET", "/refunds?status=waiting", "manager@nationsbestcpr.com")
done = call("POST", f"/refunds/{refund['id']}/approve", "manager@nationsbestcpr.com")
# done["status"] == "succeeded" (or "processing" while Stripe works)
```

### Find a student, then move their seat (curl)

```bash
curl -s "$BASE/students?q=dana%40example.com" \
  -H "Authorization: Bearer $BOOKING_ENGINE_API_KEY" -H "X-Acting-User: cs.person@nationsbestcpr.com"

curl -s "$BASE/bookings/$SEAT/move-options" -H "Authorization: Bearer $BOOKING_ENGINE_API_KEY" \
  -H "X-Acting-User: cs.person@nationsbestcpr.com"

curl -s -X POST "$BASE/bookings/$SEAT/move" -H "Authorization: Bearer $BOOKING_ENGINE_API_KEY" \
  -H "X-Acting-User: cs.person@nationsbestcpr.com" -H "Content-Type: application/json" \
  -d '{"to_class": "'$CLASS'", "product_id": 3, "reason": "customer_request", "fee_cents": 0}'
```

### Book by phone (JavaScript)

```js
const res = await fetch(`${BASE}/classes/${classId}/bookings`, {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.BOOKING_ENGINE_API_KEY}`,
             "X-Acting-User": staffEmail, "Content-Type": "application/json" },
  body: JSON.stringify({
    how: "phone",                           // or "invoice" with company, due_on, po_number
    product_id: 3,
    buyer: { first_name: "Pat", last_name: "Caller", email: "pat@example.com", phone: "(804) 555-0190" },
    attendees: [{ buyer: true }, { first_name: "Sam", last_name: "Lee", email: "sam@example.com" }],
  }),
});
const { data } = await res.json();          // data.payment_url goes to the buyer by text and email (GHL)
```

## Security notes

- Keys go in Replit Secrets only. One key per tool, so one can be switched
  off without touching the others.
- A key never does more than the person named on the call may do, and the
  database refuses large refunds and extra seats for anyone without those
  permissions in that city.
- The API sends no browser cookies and needs no form token. It never returns
  card details or a class's private details (access codes, on-site contacts).
