"""Every instructor field the NBC Master Base holds.

Revision ID: 0011
Revises: 0010
Created: 2026-11-20

The SQL is migrations/sql/0011_instructor_data_from_airtable.sql (frozen). Adds:
  * instructors: Airtable's Instructors fields (address, availability, outreach, removal, HUB, page type ...);
    the email becomes optional;
  * instructor_metros, instructor_hub_members, instructor_locations;
  * location_instructor_settings (Master Locations' instructor fields, staff only);
  * session_instructor_details (the class's lead matched to an instructor, planning fields);
  * recruiting_needs and recruiting_need_classes (Instructor Recruiting Needs);
  * the three r.25 certification types in cert_type_course_families, and the instructor_cert_status view.
"""

from migrations.sqlfile import run_sql, run_sql_file

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_COLUMNS = ("full_name hub_key city state postal_code active_in_enrollware is_hub_instructor "
            "interested_in_leading_hub additional_certs certs_last_synced_on available_days available_times "
            "classes_per_month locations_to_avoid notice_needed availability_notes availability_updated_on "
            "availability_updated_by outreach_status last_contact_on next_follow_up_on outreach_notes "
            "removal_recommended_on removal_reason removal_status remove_from associate page_type "
            "date_approval_url other_enrollware_accounts source_synced_at source_deleted_at").split()


def upgrade() -> None:
    run_sql_file("0011_instructor_data_from_airtable.sql")


def downgrade() -> None:
    run_sql("""
        DROP VIEW instructor_cert_status;
        DELETE FROM cert_type_course_families
         WHERE cert_type IN ('ARC_FA_CPR_AED_INSTRUCTOR_R25', 'ARC_BLS_INSTRUCTOR_R25', 'ARC_INSTRUCTOR_TRAINER_R25');
        DROP TABLE recruiting_need_classes;
        DROP TABLE recruiting_needs;
        DROP TABLE session_instructor_details;
        DROP TABLE location_instructor_settings;
        DROP TABLE instructor_locations;
        DROP TABLE instructor_hub_members;
        DROP TABLE instructor_metros;
        DELETE FROM instructors WHERE email IS NULL;
        ALTER TABLE instructors ALTER COLUMN email SET NOT NULL;
        COMMENT ON TABLE instructors IS '[OWNED from portal launch; MIRROR: Airtable before] Teaching profile used for eligibility, ranking and pay. Linked to a user account (role "instructor") once invited to the portal. Lead vs helper is per class, in assignments.role.';
    """ + "".join(f"ALTER TABLE instructors DROP COLUMN {c};\n" for c in _COLUMNS))
