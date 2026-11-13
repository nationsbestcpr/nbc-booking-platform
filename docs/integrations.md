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

Without `contact.nbc_invite_link` the engine can't send invites: the job
stops with that message on the Integrations page, and Retry sends it once the
field exists.

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

## 3. Workflows to build from the templates

| Workflow | Trigger | Sends |
|---|---|---|
| Attendee invitation | Contact tag added: `nbc-send-invite` | Invitation template, with `{{contact.nbc_booked_by}}` and the button link `{{contact.nbc_invite_link}}` |
| Booking confirmation | Opportunity created in Community Class, stage Pending | Confirmation template (class fields above) |
| Booked-for-you note to the booker (optional) | Contact tag added: `nbc-booker` | "Your team is booked" template |

Keep new workflows switched off in the NBC sub-account until the Jan 6–8
switch-over (launch plan 10b).
