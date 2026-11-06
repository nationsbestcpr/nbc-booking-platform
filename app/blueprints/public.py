"""Landing page: the classes that are on sale, for everyone.

Signed-in people also get a short greeting based on their role, worked out
from their account (nobody chooses a role). Book opens /book/<class> (book.py).

Only data safe for the public is selected (app/classes.py): never access
codes, on-site contacts or payment details (those live in *_private_details tables).
"""

from __future__ import annotations

from flask import Blueprint, render_template, request

from .. import auth, classes

bp = Blueprint("public", __name__)


@bp.get("/")
def home():
    """Upcoming classes in every city, page by page (1, 2, 3 ... N), soonest first.

    The search itself (filters, classes near the visitor, pages) is
    classes.search(), shared with the customer dashboard; see app/classes.py."""
    found = classes.search(classes.filters_from(request.args))
    user = auth.current_user()
    my_classes = classes.next_booked(user.id, user.email, user.email_verified) if user else []
    return render_template("public/home.html", found=found, my_classes=my_classes,
                           welcome=request.args.get("welcome") == "1")
