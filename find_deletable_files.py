#
# Finds files listed in fsnebula database that are safe to remove and dumps the
# filenames to a text file.
#
# NOTE: This script takes a lot of time and resources to run! Use carefully!!
#
# Steps taken:
#  1) Find mods that are marked as deleted (hidden) and get list of packages
#     used by each. Releases updated in the last RECENT_DAYS days are treated
#     as visible, since they may be chunked uploads that haven't finished yet.
#  2) Find mods that are NOT marked as deleted and get list of packages used by
#     each, plus the master copy of every duplicate and every image in use.
#  3) Find packages unique to the deleted list.
#  4) Check for files on disk.
#  5) Output list of files on disk that should be safe to delete.
#
# Usage example (as root/sudo):
#  # cd /home/nebula/fsnebula
#  # source .venv/bin/activate
#  # export NEBULA_SETTINGS=`pwd`/../production.cfg
#  # python find_deletable_files.py `pwd`/../uploads
# 
# To delete files from disk (as root/sudo):
#  # cd /home/nebula/uploads
#  # cat /tmp/deletable_files.txt | while read file; do rm -f $file; done;
# 

from app.models import Mod, ModRelease, UploadedFile
from mongoengine.queryset.visitor import Q
from datetime import datetime, timedelta
import sys
import os
import stat

RECENT_DAYS = 30
RECENT_CUTOFF = datetime.utcnow() - timedelta(days=RECENT_DAYS)

if len(sys.argv) < 2:
  print("Find safely deletable files from fsnebula database.\n")
  print(f"Usage: {sys.argv[0]} [-d] <path_to_uploads>\n")
  print(" Options:")
  print("  -d      Output extra details about files to be removed")
  print("")
  sys.exit(0)

if not os.path.isdir(sys.argv[-1]):
  sys.exit("ERROR: Invalid or missing upload path!")


def human_readable_size(size, decimal_places=2):
  for unit in ['bytes', 'KiB', 'MiB', 'GiB', 'TiB']:
      if size < 1024.0 or unit == 'TiB':
          break
      size /= 1024.0
  return f"{size:.{decimal_places}f} {unit}"


def find_visible_packages(checksums: set):
  visible = ModRelease.objects(Q(hidden=False) | Q(last_update=None) |
    Q(last_update__gte=RECENT_CUTOFF))
  for rel in visible:
    mod = rel.mod
    for pkg in rel.packages:
      for archive in pkg.files:
        checksums.add(archive.checksum)
        ref = UploadedFile.objects(checksum=archive.checksum).first()
        # render_mod_list serves the master copy in place of a duplicate
        if ref and ref.duplicate_of:
          checksums.add(ref.duplicate_of)

  for rel in ModRelease.objects.only('banner', 'screenshots', 'attachments'):
    checksums.update([rel.banner] + rel.screenshots + rel.attachments)

  for mod in Mod.objects.only('logo', 'tile'):
    checksums.update([mod.logo, mod.tile])


def find_hidden_packages(checksums: set):
  hidden = ModRelease.objects(hidden=True, last_update__lt=RECENT_CUTOFF)
  for rel in hidden:
    mod = rel.mod
    for pkg in rel.packages:
      for archive in pkg.files:
        ref = UploadedFile.objects(checksum=archive.checksum).first()
        if ref and ref.filename and ref.filesize:
          checksums.add(ref.checksum)


def gen_deletable_details():
  try:
    with open('/tmp/deletable_files.txt', 'r') as file:
      my_list = file.read().splitlines()
      # convert filenames into checksums
      my_list[:] = [s.replace('public/', '') for s in my_list]
      my_list[:] = [s.replace('/', '') for s in my_list]  # strip separators
      my_list[:] = [s.split('.')[0] for s in my_list]  # strip off extensions

      try:
        with open('/tmp/deletable_files_detail.csv', 'w') as detail:
          print("Saving details to: /tmp/deletable_files_detail.csv ...",
            end=" ")
          # start with header for columns
          print(f"ModID\tVersion\tFilename\tFilesize\tSpecial", file=detail)
          hidden = ModRelease.objects(hidden=True)
          for rel in hidden:
            mod = rel.mod
            for pkg in rel.packages:
              for archive in pkg.files:
                if archive.checksum in my_list:
                  my_list.remove(archive.checksum)
                  print(f"{mod.id}\t{rel.version}\t{archive.filename}\t"
                    f"{archive.filesize}\t{'private' if rel.private else ''}",
                    file=detail)
          print("Done.")
      except PermissionError:
          print("ERROR: Permission failure attempting to create "
            "/tmp/deletable_files_detail.csv.")
      except OSError as e:
          print(f"ERROR: OS error writing /tmp/deletable_files_detail.csv: {e}")
      except Exception as e:
          print("ERROR: Unexpected error writing "
            f"/tmp/deletable_files_detail.csv: {e}")
  except PermissionError:
      print("ERROR: Permission failure attempting to access "
        "/tmp/deletable_files.txt.")
  except OSError as e:
      print(f"ERROR: OS error reading /tmp/deletable_files.txt: {e}")
  except Exception as e:
      print(f"ERROR: Unexpected error reading /tmp/deletable_files.txt: {e}")


def find_deletable_files():
  hidden = set()
  visible = set()

  find_hidden_packages(hidden)
  find_visible_packages(visible)

  unique = hidden - visible

  results = []
  for item in unique:
    ref = UploadedFile.objects(checksum=item).first()
    if ref:
      results.append((ref.filename, ref.filesize))

  total_size = sum(item[1] for item in results)

  print(f"Found {len(unique)} reference files totaling "
    f"{human_readable_size(total_size)}.")

  print("Checking files on disk...")

  total_size = 0
  for item in results[:]:
    filepath = os.path.join(sys.argv[-1], item[0])
    if os.path.exists(filepath):
      stats = os.stat(filepath)
      total_size += stats.st_size
    else:
      # file doesn't exist so it's probably been deleted previously
      results.remove(item)

  if total_size == 0:
    print("No deletable files found.")
    sys.exit(0)
  else:
    print(f"Verified {len(results)} deletable files totaling "
      f"{human_readable_size(total_size)}.")

    try:
      with open('/tmp/deletable_files.txt', 'w') as f:
        print("Saving filenames to: /tmp/deletable_files.txt ...", end=" ")
        for item in results:
          print(item[0], file=f)
        print("Done.")
    except PermissionError:
        sys.exit("ERROR: Permission failure attempting to create "
          "/tmp/deletable_files.txt.")
    except OSError as e:
        sys.exit(f"ERROR: OS error writing /tmp/deletable_files.txt: {e}")
    except Exception as e:
        sys.exit("ERROR: Unexpected error writing /tmp/deletable_files.txt: "
          f"{e}")


find_deletable_files()

if "-d" in sys.argv:
  gen_deletable_details()
