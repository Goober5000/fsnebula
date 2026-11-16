#!/usr/bin/env python
"""Mark uploaded files as cold storage and rebuild the private repo caches that list them.

Flags only hashes that are in both lists: the fresh cold_move.txt from
check_cold_candidates.py, and the sha256sum list verified on the cold host.
Dry run unless --apply is given. --unflag reverses a previous run.

After --apply or --unflag, run gen_repo.py to rebuild repo.json.
"""

import os
import re
import sys
import time
import argparse

HEX_RE = re.compile(r'^[0-9a-f]{64}$')


def read_hashes(path):
    """Read the first whitespace-separated column of each line."""
    hashes = set()
    with open(path) as stream:
        for num, line in enumerate(stream, 1):
            parts = line.split()
            if not parts:
                continue
            h = parts[0].lower()
            if not HEX_RE.match(h):
                sys.exit('%s:%d: not a SHA-256: %r' % (path, num, parts[0]))
            hashes.add(h)

    return hashes


def check_record(h, uf):
    """Return the reason a hash can't be flagged, or None."""
    if uf is None:
        return 'no UploadedFile record'
    if uf.get('expires') != -1:
        return 'not permanent'
    if uf.get('filename') != 'public/%s/%s/%s' % (h[:2], h[2:4], h[4:]):
        return 'unexpected filename %r' % uf.get('filename')
    return None


def affected_private_mods(hashes, ModRelease, UploadedFile):
    """Mod ids whose private repo cache lists any of these hashes, directly or through duplicate_of."""
    using = set(hashes)
    for doc in UploadedFile._get_collection().find({'duplicate_of': {'$in': list(hashes)}}, {'_id': 1}):
        using.add(doc['_id'])

    # The private caches only contain non-hidden private releases
    query = {'private': True, 'hidden': {'$ne': True}, 'packages.files.checksum': {'$in': list(using)}}
    return sorted(set(doc['mod'] for doc in ModRelease._get_collection().find(query, {'mod': 1})))


def regenerate(mod_ids, app, Mod, generate_private_repo):
    """Rebuild each mod's private caches; return the ids that failed."""
    cache = os.path.join(app.config['FILE_STORAGE'], 'cache')
    failed = []
    for mid in mod_ids:
        mod = Mod.objects(mid=mid).first()
        if mod is None:
            failed.append(mid)
            continue

        started = time.time()
        # generate_private_repo logs and swallows its own errors, so check the files it should have replaced
        generate_private_repo(mod)
        for pattern in ('mod_%s.json', 'mod2_%s.json'):
            path = os.path.join(cache, pattern % mid)
            if not os.path.isfile(path) or os.path.getmtime(path) < started - 1:
                failed.append(mid)
                break

    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('move', help='cold_move.txt from a fresh run of check_cold_candidates.py')
    parser.add_argument('verified', help='the sha256sum list verified on the cold host (cold_check.txt)')
    parser.add_argument('--apply', action='store_true', help='write the changes (default: dry run)')
    parser.add_argument('--unflag', action='store_true', help='clear the cold flag from these hashes instead')
    parser.add_argument('--log', default='cold_flagged.txt', help='where --apply records the hashes it changed (default: %(default)s)')
    args = parser.parse_args()

    from app import app
    from app.models import Mod, ModRelease, UploadedFile
    from app.controllers.mod import generate_private_repo

    move = read_hashes(args.move)
    verified = read_hashes(args.verified)
    wanted = move & verified
    print('%d hashes in %s, %d in %s, %d in both.' % (len(move), args.move, len(verified), args.verified, len(wanted)))
    if move - verified:
        print('%d cleared hashes were not verified on the cold host and will be skipped.' % len(move - verified))

    records = {}
    for doc in UploadedFile._get_collection().find({'_id': {'$in': list(wanted)}},
                                                    {'filename': 1, 'expires': 1, 'storage': 1, 'filesize': 1}):
        records[doc['_id']] = doc

    problems = {}
    for h in wanted:
        reason = check_record(h, records.get(h))
        if reason:
            problems[h] = reason

    for h in sorted(problems):
        print('  skip %s: %s' % (h, problems[h]))

    target = None if args.unflag else 'cold'
    change = sorted(h for h in wanted if h not in problems and records[h].get('storage') != target)
    unchanged = len(wanted) - len(problems) - len(change)
    size = sum(records[h].get('filesize') or 0 for h in change)
    print('%s %d files (%.1f GB); %d already %s; %d skipped.' % (
        'Unflag' if args.unflag else 'Flag', len(change), size / 1e9, unchanged,
        'unflagged' if args.unflag else 'cold', len(problems)))

    mod_ids = affected_private_mods(change, ModRelease, UploadedFile)
    print('%d mods have private caches to rebuild.' % len(mod_ids))

    store = app.config['FILE_STORAGE']
    cache = os.path.join(store, 'cache')
    for path in (store, cache):
        if not os.access(path, os.W_OK):
            sys.exit('Cannot write to %s; run as its owner or with sudo.' % path)

    if not args.apply:
        print('\nDry run; nothing changed. Re-run with --apply to write.')
        return

    if not change:
        print('Nothing to change.')
        return

    with open(args.log, 'a') as stream:
        stream.write('# %s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), 'unflag' if args.unflag else 'flag'))
        for h in change:
            stream.write(h + '\n')

    if args.unflag:
        result = UploadedFile._get_collection().update_many({'_id': {'$in': change}}, {'$unset': {'storage': ''}})
    else:
        result = UploadedFile._get_collection().update_many({'_id': {'$in': change}}, {'$set': {'storage': 'cold'}})
    print('Updated %d UploadedFile records (logged in %s).' % (result.modified_count, args.log))

    failed = regenerate(mod_ids, app, Mod, generate_private_repo)
    print('Rebuilt private caches for %d mods.' % (len(mod_ids) - len(failed)))
    if failed:
        print('FAILED for %d mods (see the app log): %s' % (len(failed), ' '.join(failed)))

    open(os.path.join(store, 'repo_needs_update'), 'w').close()
    print('Touched repo_needs_update. Now run gen_repo.py.')

    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
