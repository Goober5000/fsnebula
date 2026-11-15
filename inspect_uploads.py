#!/usr/bin/env python
"""Show the UploadedFile record, size on disk and using releases for a list of hashes.

Read-only. The input file has one SHA-256 per line; only the first tab-separated
column is used, so cold_check_report.tsv works as input too.
"""

import os.path
import re
import sys
import argparse

HEX_RE = re.compile(r'^[0-9a-f]{64}$')


def describe_release(rel):
    mod = rel.get('mod')
    flags = ''
    if rel.get('hidden'):
        flags += ' hidden'
    if rel.get('private'):
        flags += ' private'

    date = rel['last_update'].date() if rel.get('last_update') else 'no-date'
    return '%s %s %s%s' % (getattr(mod, 'id', mod), rel.get('version'), date, flags)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('hashes', help='file with one SHA-256 per line')
    parser.add_argument('--out', help='write the TSV here instead of to stdout')
    args = parser.parse_args()

    hashes = []
    with open(args.hashes) as stream:
        for line in stream:
            h = line.split('\t')[0].strip().lower()
            if HEX_RE.match(h):
                hashes.append(h)

    from app import app
    from app.models import UploadedFile, ModRelease

    files = UploadedFile._get_collection()
    releases = ModRelease._get_collection()
    store = app.config['FILE_STORAGE']

    out = open(args.out, 'w') if args.out else sys.stdout
    out.write('sha256\tdb_filesize\tdisk_size\texpires\tfilename\tduplicates\treleases\n')

    no_record = not_on_disk = 0
    for h in hashes:
        uf = files.find_one({'_id': h})
        if uf is None:
            no_record += 1
            uf = {}

        filename = uf.get('filename')
        path = os.path.join(store, filename) if filename else None
        if path and os.path.isfile(path):
            disk_size = str(os.path.getsize(path))
        else:
            disk_size = 'NOT ON DISK'
            not_on_disk += 1

        dups = [d['_id'] for d in files.find({'duplicate_of': h}, {'_id': 1})]
        used = [describe_release(r) for r in releases.find(
            {'packages.files.checksum': {'$in': [h] + dups}},
            {'mod': 1, 'version': 1, 'last_update': 1, 'hidden': 1, 'private': 1})]

        out.write('\t'.join([
            h,
            str(uf.get('filesize', 'missing')),
            disk_size,
            str(uf.get('expires')),
            str(filename),
            ','.join(dups),
            '; '.join(sorted(used)),
        ]) + '\n')

    if args.out:
        out.close()

    sys.stderr.write('%d hashes: %d without an UploadedFile record, %d not on disk.\n' % (
        len(hashes), no_record, not_on_disk))


if __name__ == '__main__':
    main()
