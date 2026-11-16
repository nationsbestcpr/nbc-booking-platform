# GHL setup for booking emails and tracking

What the GHL sub-account needs so the engine's data reaches it, and so its
workflows can send the emails built from the NBC templates. Do this in
Sandbox 2 first; the Dec 14 snapshot carries it to the NBC sub-account.

## 1. Custom fields

Create these (Settings → Custom Fields), then press **Load custom fields** on
the engine's Integrations page (the engine also looks them up when it starts).
The engine matches them by key, so the key must be exactly this.

**Opportunity fields**

| Key | Filled with |
|---|---|
| `opportunity.booked_by` | Name of the person who booked and paid |
| `opportunity.booked_by_email` | Their email |
| `opportunity.booking_type` | `Booked by themselves`, `Booked by someone else` or `Bring a friend` |
| `opportunity.addons` | Add-ons bought for this seat, e.g. `Laminated wallet card` |
| any key you give a booking question | That question's answer (Admin → Booking questions → GHL field) |

These are in addition to the class fields the engine already fills
(`opportunity.class_id`, `course`, `course_date`, `course_location`, `hours`,
`instructor`, `reg_type`, `class_price`, `order_total`, `delivery`, `reg_date`).

**Contact fields** (read by the invitation email)

| Key | Filled with |
|---|---|
| `contact.nbc_invite_link` | The attendee's personal link to make their account |
| `contact.nbc_booked_by` | Name of the person who booked them a seat |
| `contact.nbc_payment_link` | A Stripe payment link customer service sent (a phone booking, or a class change's price difference) |
| `contact.nbc_payment_amount` | The amount due on that link, e.g. `108.00` |
| `contact.nbc_offer_link` | A waiting-list seat offer: the link to pay for the held seat |
| `contact.nbc_offer_expires` | When the offer runs out, in the class's time, e.g. `2:15 PM on Sat, Jan 16` |

**Create missing fields in GHL** on the Integrations page makes any of these
that don't exist yet (NBC Payment Link, NBC Payment Amount, NBC Offer Link,
NBC Offer Expires are the new ones).

Without `contact.nbc_invite_link` the engine can't send invites: the job
stops with that message on the Integrations page, and Retry sends it once the
field exists. The same goes for the payment link and offer fields.

**Pipeline stage:** the Community Class pipeline needs a **Waitlisted** stage
(besides Pending, Complete, No Show and Cancelled). Someone who joins a
waiting list gets an opportunity there; it moves to Pending when they pay for
an offered seat, and to Cancelled if the list closes with no seat.

## 2. Tags the engine adds

Names are in Business settings → **GHL tags**; change one there if a workflow
uses another name.

| Tag | On | When |
|---|---|---|
| `nbc-student` | every attendee | each booked seat |
| `nbc-booked-self` | an attendee who booked their own seat | each booked seat |
| `nbc-booked-by-someone-else` | an attendee someone else booked for | each booked seat |
| `nbc-booker` | the person who paid for other people's seats | when they book for others |
| `nbc-bring-a-friend` | a friend's seat | each booked friend seat |
| `nbc-course-<course>` | every attendee | e.g. `nbc-course-bls`, `nbc-course-first-aid-cpr-aed` |
| `nbc-city-<city>` | every attendee | e.g. `nbc-city-richmond-va` |
| `nbc-addon-<code>` | an attendee with that add-on | e.g. `nbc-addon-wallet-card` |
| `nbc-send-invite` | an attendee someone else booked for | removed and added again each time an invite goes out |
| `nbc-send-payment-link` | the buyer | each time customer service sends a payment link (removed and added again) |
| `nbc-class-moved` | the attendee | each time their seat moves to another class (the opportunity already has the new date and place) |
| `nbc-waitlist` | someone who joined a waiting list | when they join |
| `nbc-waitlist-offer` | someone waiting | a seat is held for them: the offer link and expiry are on the contact |
| `nbc-offer-expired` | someone waiting | they didn't pay within 2 hours; they keep their place |
| `nbc-waitlist-no-seat` | someone waiting | the list closed (cut-off) and no seat opened |

## 3. Workflows to build from the templates

| Workflow | Trigger | Sends |
|---|---|---|
| Attendee invitation | Contact tag added: `nbc-send-invite` | Invitation template, with `{{contact.nbc_booked_by}}` and the button link `{{contact.nbc_invite_link}}` |
| Booking confirmation | Opportunity created in Community Class, stage Pending | Confirmation template (class fields above) |
| Booked-for-you note to the booker (optional) | Contact tag added: `nbc-booker` | "Your team is booked" template |
| Payment link | Contact tag added: `nbc-send-payment-link` | Email and SMS with `{{contact.nbc_payment_link}}` and `{{contact.nbc_payment_amount}}` |
| Class changed | Contact tag added: `nbc-class-moved` | New date, time and place (opportunity fields `course_date`, `course_location`) |
| Joined the waiting list | Contact tag added: `nbc-waitlist` | "You're on the list": 2 hours to pay if a seat opens, offers 8 AM – 6 PM class time |
| Seat offer | Contact tag added: `nbc-waitlist-offer` | Email and SMS: pay by `{{contact.nbc_offer_expires}}` at `{{contact.nbc_offer_link}}` |
| Offer expired | Contact tag added: `nbc-offer-expired` | "The time ran out; you're still on the list" |
| No seat opened | Contact tag added: `nbc-waitlist-no-seat` | "No seat opened; here are other dates" |
| Cancellation | Opportunity stage changed to Cancelled | Cancellation note (refund, if any, comes from Stripe) |
| Class result | Opportunity stage changed to Complete / No Show | Certificate-is-coming note / "book another date" note |

Keep new workflows switched off in the NBC sub-account until the Jan 6–8
switch-over (launch plan 10b).
