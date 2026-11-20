# Leaving Enrollware: the cutover

The booking app is ready to run with Enrollware switched off (migration 0012). On cutover day:

1. **Deploy** main to Replit and run `.venv/bin/python -m flask --app wsgi db upgrade` (0012 only adds
   tables, columns and settings). `pip install -r requirements/app.txt` once, for openpyxl (reading Enrollware's
   .xlsx export).
2. **Stop new bookings in Enrollware** (unpublish its classes).
3. **Superadmin → Cutover and import → "Move every city to the booking app"**. Every city books here from now on,
   and new cities from Airtable start here too (`enrollware_retired`).
4. **Export registrations from Enrollware** (Excel) for classes from today on, then **Check the file** on the same
   page. The check runs the whole import and undoes it, and lists rows that need a look:
   * *No location in the booking app has this name*: the Enrollware location name doesn't match a Master Locations
     name whose classes are on sale. Fix the name in Airtable (or put the class on sale), Sync now, check again.
   * *No class at this location and time*: the class isn't on sale here (Status needs "Enrollware" or
     "Booking App").
   * *The class is full in the booking app*: tick "add an extra seat" to give each student the seat Enrollware
     sold them (recorded as a seat override with the reason).
   * *Already has a seat in this course*: the same email twice; book the second person by hand.
5. **Import**. Pending students get their seat, paid as in Enrollware (a manual payment "Enrollware CLS-…"), or
   awaiting payment when Enrollware showed a balance due (due on class day; the Ops Hub records the payment or sends
   a link). Waitlisted students keep their place. Nobody is messaged. Tick "send to GHL" only if Enrollware's
   students aren't in GHL already.
6. Loading a newer export later adds only what's new (each student once).
7. In Airtable, the status "Booking App" puts a class on sale like "Enrollware" did, so it can be renamed whenever.

The same import runs from the shell: `flask --app wsgi enrollware import export.xlsx --as you@nationsbestcpr.com`
(add `--commit` to import, `--add-seats`, `--send-to-ghl`).

## Instructors

* Instructors come from Airtable's Instructors table; who teaches each class is ASSIGN INSTRUCTOR. Each sync puts
  the class in that instructor's portal.
* An instructor signs up (Create account → Instructor), then a Superadmin gives the Instructor role on
  **Superadmin → Instructor accounts** ("Give the role to all that match Airtable"). The account links to the
  Airtable record by email.
* In the portal: confirm classes 1–5 days out; "I can't teach it" (NBC finds someone and changes ASSIGN INSTRUCTOR;
  the class stays theirs until then); availability changes (a change that rules out an assigned class asks, for
  each one, whether they'll still teach it); and on class day the sign-in sheet, where students sign on the
  instructor's phone or tablet (signing checks them in), then Complete / No show and Finish.
* The Ops Hub reads confirmations in `/api/v1/classes` and requests in `/api/v1/instructor-requests`, applies them
  in Airtable, and closes them.
