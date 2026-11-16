#!/usr/bin/env python
"""Check cold storage candidates against every release in the database.

Read-only. Writes the hashes that are safe to move (cold_move.txt) and a report
explaining every decision (cold_check_report.tsv).
"""

import os.path
import argparse
from collections import Counter
from datetime import datetime

import semantic_version

KEEP = 'KEEP'
REVIEW = 'REVIEW'
MOVE = 'MOVE'


def ref_id(value):
    # LazyReferenceField stores the raw primary key unless dbref=True
    return getattr(value, 'id', value)


def parse_version(version):
    try:
        return semantic_version.Version(version)
    except (ValueError, TypeError):
        return None


def mark_latest(releases):
    """Flag the highest visible version of each mod, and hidden releases above it."""
    by_mod = {}
    for rel in releases:
        rel['semver'] = parse_version(rel['version'])
        rel['latest'] = False
        rel['hidden_newer'] = False
        by_mod.setdefault(rel['mid'], []).append(rel)

    best = {}
    for mid, rels in by_mod.items():
        versions = [r['semver'] for r in rels if not r['hidden'] and r['semver'] is not None]
        if not versions:
            continue

        best[mid] = max(versions)
        for rel in rels:
            # Ties (e.g. differing only in build metadata) all count as latest
            if not rel['hidden'] and rel['semver'] is not None and not rel['semver'] < best[mid]:
                rel['latest'] = True

    for rel in releases:
        if rel['hidden'] and rel['semver'] is not None and rel['mid'] in best and rel['semver'] > best[rel['mid']]:
            rel['hidden_newer'] = True


def classify(candidates, uploads, releases, image_users, cutoff):
    """Return {hash: (decision, [(kind, detail)], [note])} for every candidate."""
    mark_latest(releases)

    users = {}
    for rel in releases:
        for h in rel['hashes'] & candidates:
            users.setdefault(h, []).append(rel)

    result = {}
    for h in candidates:
        keep, review, notes = [], [], []
        up = uploads.get(h)

        if up is None:
            keep.append(('missing-record', 'no UploadedFile record'))
        else:
            if up.get('expires') != -1:
                keep.append(('not-permanent', 'UploadedFile is not in public/'))
            if up.get('duplicate_of'):
                review.append(('duplicate', 'is a duplicate of %s' % up['duplicate_of']))

        rels = users.get(h, [])
        for rel in rels:
            label = '%s %s' % (rel['mid'], rel['version'])
            if rel['private']:
                label += ' (private)'

            if rel['hidden']:
                if rel['hidden_newer']:
                    review.append(('hidden-newer', 'hidden release newer than latest: %s' % label))
                elif rel['date'] is not None and rel['date'] > cutoff:
                    review.append(('hidden-recent', 'hidden release from %s: %s' % (rel['date'], label)))
                continue

            if rel['latest']:
                keep.append(('latest', 'latest version: %s' % label))
            if rel['date'] is None:
                review.append(('no-date', 'release has no date: %s' % label))
            elif rel['date'] > cutoff:
                keep.append(('recent', 'released %s: %s' % (rel['date'], label)))
            if rel['semver'] is None:
                review.append(('bad-version', 'unparseable version: %s' % label))

        for user in image_users.get(h, []):
            keep.append(('image', 'used as image by %s' % user))

        visible = [r for r in rels if not r['hidden']]
        if not rels:
            notes.append('not used by any release')
        elif not visible:
            notes.append('only used by hidden releases')
        if any(r['private'] for r in visible):
            notes.append('used by a private release')

        if keep:
            decision = KEEP
        elif review:
            decision = REVIEW
        else:
            decision = MOVE

        reasons = list(dict.fromkeys(keep + review))
        result[h] = (decision, reasons, notes)

    return result


def load_from_db(candidates):
    from app.models import Mod, ModRelease, UploadedFile

    uploads = {}
    projection = {'filename': 1, 'filesize': 1, 'expires': 1, 'duplicate_of': 1}
    for doc in UploadedFile._get_collection().find({'_id': {'$in': list(candidates)}}, projection):
        uploads[doc['_id']] = doc

    # render_mod_list serves the master copy in place of a duplicate
    duplicate_of = {}
    for doc in UploadedFile._get_collection().find({'duplicate_of': {'$nin': [None, '']}}, {'duplicate_of': 1}):
        duplicate_of[doc['_id']] = doc['duplicate_of']

    releases = []
    image_users = {}
    projection = {
        'mod': 1, 'version': 1, 'last_update': 1, 'hidden': 1, 'private': 1,
        'banner': 1, 'screenshots': 1, 'attachments': 1,
        'packages.files.checksum': 1, 'packages.files.urls': 1,
    }
    for doc in ModRelease._get_collection().find({}, projection):
        rel = {
            'mid': ref_id(doc.get('mod')),
            'version': doc.get('version'),
            'date': doc['last_update'].date() if doc.get('last_update') else None,
            'hidden': bool(doc.get('hidden')),
            'private': bool(doc.get('private')),
            'hashes': set(),
        }

        for pkg in doc.get('packages') or []:
            for ar in pkg.get('files') or []:
                if ar.get('urls') or not ar.get('checksum'):
                    continue
                rel['hashes'].add(duplicate_of.get(ar['checksum'], ar['checksum']))

        for prop in ('banner', 'screenshots', 'attachments'):
            values = doc.get(prop) or []
            if isinstance(values, str):
                values = [values]
            for value in values:
                if value in candidates:
                    image_users.setdefault(value, []).append('%s %s %s' % (rel['mid'], rel['version'], prop))

        releases.append(rel)

    for doc in Mod._get_collection().find({}, {'logo': 1, 'tile': 1}):
        for prop in ('logo', 'tile'):
            if doc.get(prop) in candidates:
                image_users.setdefault(doc[prop], []).append('mod %s %s' % (doc['_id'], prop))

    return uploads, releases, image_users


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('candidates', help='file with one SHA-256 per line')
    parser.add_argument('--cutoff', default='2024-12-31', help='newest release date allowed to move, inclusive (default: %(default)s)')
    parser.add_argument('--out-dir', default='.', help='where to write the output files (default: current directory)')
    args = parser.parse_args()

    cutoff = datetime.strptime(args.cutoff, '%Y-%m-%d').date()
    with open(args.candidates) as stream:
        candidates = set(line.strip().lower() for line in stream if line.strip())

    print('Loaded %d candidate hashes, cutoff %s.' % (len(candidates), cutoff))
    print('Reading database...')
    uploads, releases, image_users = load_from_db(candidates)
    print('Found %d of them in UploadedFile; scanned %d releases.' % (len(uploads), len(releases)))

    result = classify(candidates, uploads, releases, image_users, cutoff)

    move_path = os.path.join(args.out_dir, 'cold_move.txt')
    report_path = os.path.join(args.out_dir, 'cold_check_report.tsv')

    counts = Counter()
    sizes = Counter()
    kinds = Counter()
    with open(move_path, 'w') as move, open(report_path, 'w') as report:
        report.write('sha256\tdecision\tfilesize\treasons\tnotes\n')
        for h in sorted(result):
            decision, reasons, notes = result[h]
            size = (uploads.get(h) or {}).get('filesize') or 0
            counts[decision] += 1
            sizes[decision] += size
            for kind in set(kind for kind, _ in reasons):
                kinds[(decision, kind)] += 1

            if decision == MOVE:
                move.write(h + '\n')

            report.write('%s\t%s\t%d\t%s\t%s\n' % (
                h, decision, size, '; '.join(detail for _, detail in reasons), '; '.join(notes)))

    print()
    for decision in (MOVE, REVIEW, KEEP):
        print('%-6s %5d hashes  %8.1f GB' % (decision, counts[decision], sizes[decision] / 1e9))
        for (dec, kind), n in sorted(kinds.items()):
            if dec == decision:
                print('         %-15s %5d' % (kind, n))

    print()
    print('Wrote %s and %s.' % (move_path, report_path))


if __name__ == '__main__':
    main()
