#!/usr/bin/env python3
"""Standalone DB migration runner.

Used by the skytrack-firstboot.service oneshot and by the operator to
rerun migrations after an OTA update. Safe to invoke repeatedly: every
DDL statement is `CREATE IF NOT EXISTS` and version inserts are
`INSERT OR IGNORE`.

Usage:
    sudo -u skytrack python3 scripts/db_migrate.py          # apply
    sudo -u skytrack python3 scripts/db_migrate.py --info   # show version
    sudo -u skytrack python3 scripts/db_migrate.py --prune  # also prune

Honors environment variables:
    SKYTRACK_DB_PATH   path to skytrack.db (default /var/lib/skytrack/skytrack.db)
"""

import argparse
import logging
import os
import sys

# Make the project root importable when invoked as scripts/db_migrate.py
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description='SkyTrack DB migrator')
    ap.add_argument('--info', action='store_true', help='just print schema version')
    ap.add_argument('--prune', action='store_true', help='also prune retention windows')
    ap.add_argument('--vacuum', action='store_true', help='also VACUUM after prune')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    path = os.environ.get('SKYTRACK_DB_PATH', db.DEFAULT_DB_PATH)
    print(f'db: {path}')

    if args.info:
        try:
            v = db.current_version()
        except Exception as exc:
            print(f'error reading schema version: {exc}', file=sys.stderr)
            return 2
        print(f'schema version: v{v}')
        return 0

    try:
        db.migrate()
    except Exception as exc:
        print(f'migration failed: {exc}', file=sys.stderr)
        return 1
    print(f'schema migrated to v{db.current_version()}')

    if args.prune:
        print('pruning retention windows ...')
        db.prune()
        print('prune complete')

    if args.vacuum:
        print('VACUUM ...')
        try:
            db.get_conn().execute('VACUUM')
            print('vacuum complete')
        except Exception as exc:
            print(f'vacuum failed: {exc}', file=sys.stderr)
            return 1

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
