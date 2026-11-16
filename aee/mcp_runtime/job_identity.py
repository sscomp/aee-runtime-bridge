"""Job identity grammar shared by generation and every persisted reader."""
import re


# A 1-16 character node code (ASCII uppercase letter, then letters/digits).
# Keep readers independent of the configured generation prefix for rollback.
_PREFIX = r'[A-Z][A-Z0-9]{0,15}JOB'
JOB_ID_PREFIX = re.compile(_PREFIX)
JOB_ID = re.compile(_PREFIX + r'-[0-9]{8}-[0-9a-f]{32}')


def validate_job_id_prefix(value):
    if not isinstance(value, str) or not JOB_ID_PREFIX.fullmatch(value):
        raise ValueError('job_id_prefix must match [A-Z][A-Z0-9]{0,15}JOB')
    return value
