#!/usr/bin/env python
"""Check cold storage files on disk and build the lists for copying and verifying them.

Read-only. Writes three files:
  cold_paths.txt     paths relative to FILE_STORAGE, for rsync --files-from
  cold_check.txt     "<sha256>  <path relative to public/>", for sha256sum -c on a mirror
  cold_problems.tsv  hashes left out of both lists, and why
"""

import os.path
import re
import sys
import hashlib
import argparse
from collections import Counter

HEX_RE = re.compile(r'^[0-9a-f]{64}$')


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)

    return h.hexdigest()


def check_one(h, uf, store, verify_hash):
    """Return (problem, disk_size); problem is None when the file is fine."""
    if uf is None:
        return 'no UploadedFile record', None
    if uf.get('expires') != -1:
        return 'not permanent', None

    expected = 'public/%s/%s/%s' % (h[:2], h[2:4], h[4:])
    if uf.get('filename') != expected:
        return 'unexpected filename %r' % uf.get('filename'), None

    path = os.path.join(store, expected)
    if not os.path.isfile(path):
        return 'not on disk', None

    disk_size = os.path.getsize(path)
    if uf.get('filesize') is not None and uf['filesize'] != disk_size:
        return 'size mismatch: db %d, disk %d' % (uf['filesize'], disk_size), disk_size

    if verify_hash and sha256_file(path) != h:
        return 'checksum mismatch on disk', disk_size

    return None, disk_size


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('hashes', help='file with one SHA-256 per line (e.g. cold_move.txt)')
    parser.add_argument('--out-dir', default='.', help='where to write the output files (default: current directory)')
    parser.add_argument('--verify-hash', action='store_true',
                        help='also re-hash every file on this server (reads the full ~830 GB)')
    args = parser.parse_args()

    with open(args.hashes) as stream:
        hashes = sorted(set(line.split('\t')[0].strip().lower() for line in stream
                            if HEX_RE.match(line.split('\t')[0].strip().lower())))

    from app import app
    from app.models import UploadedFile

    store = app.config['FILE_STORAGE']
    projection = {'filename': 1, 'filesize': 1, 'expires': 1}
    uploads = dict((doc['_id'], doc) for doc in
                   UploadedFile._get_collection().find({'_id': {'$in': hashes}}, projection))

    paths_path = os.path.join(args.out_dir, 'cold_paths.txt')
    check_path = os.path.join(args.out_dir, 'cold_check.txt')
    problems_path = os.path.join(args.out_dir, 'cold_problems.tsv')

    ok = 0
    ok_bytes = 0
    problems = Counter()
    with open(paths_path, 'w') as paths, open(check_path, 'w') as check, open(problems_path, 'w') as bad:
        bad.write('sha256\tproblem\n')
        for i, h in enumerate(hashes, 1):
            problem, disk_size = check_one(h, uploads.get(h), store, args.verify_hash)
            if problem:
                problems[problem.split(':')[0]] += 1
                bad.write('%s\t%s\n' % (h, problem))
            else:
                ok += 1
                ok_bytes += disk_size
                rel = 'public/%s/%s/%s' % (h[:2], h[2:4], h[4:])
                paths.write(rel + '\n')
                check.write('%s  %s\n' % (h, rel[len('public/'):]))

            if args.verify_hash and i % 100 == 0:
                sys.stderr.write('  %d / %d checked\n' % (i, len(hashes)))

    print('%d hashes read.' % len(hashes))
    print('OK       %5d files  %.1f GB on disk (%d bytes)' % (ok, ok_bytes / 1e9, ok_bytes))
    for problem, n in sorted(problems.items()):
        print('PROBLEM  %5d  %s' % (n, problem))
    print()
    print('Wrote %s, %s and %s.' % (paths_path, check_path, problems_path))


if __name__ == '__main__':
    main()
