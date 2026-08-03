#!/bin/bash
# One-file permission-fix probe, meant to run AS the target owner on alcatraz.
# Tests whether the NFS server lets this numeric uid chgrp a file to gid 100
# (group 'users') and chmod it group-writable. Touches exactly ONE file.
#
# Run it like:
#   sudo su - azach -c 'bash /gpfs/fs0/l/murr/new_tools/embryoDB/permission_fix_fulldb/test_alcatraz_chgrp.sh'
# (adjust the /gpfs prefix if alcatraz mounts the share elsewhere, e.g. /murrlab)
#
# Run this FIRST: a NO-GO verdict is what tells you the server's manage-gids is
# refusing the chgrp, and therefore that you need the two-pass laundering dance
# (setgid_dirs.py then launder_fix.py). See README.md in this directory.
# DEPLOYMENT-SPECIFIC: gid 100 and the default path below are this lab's.

set -u
TARGET_GID=100   # numeric on purpose — do NOT trust the name 'users' across boxes
FILE="${1:-/gpfs/fs0/u/azach/images/20160603_ceh-13_enh450_L1/dats/CA20160603_ceh-13_enh450_L1.csv}"

echo "=== who am I (server sees these numeric creds) ==="
id

echo "=== group 'users' as this box resolves it (want gid ${TARGET_GID}) ==="
getent group users || echo "(no group 'users' by name here)"
getent group "$TARGET_GID" || echo "(no group with gid ${TARGET_GID} here)"

echo "=== filesystem backing the target ==="
df -T "$FILE" 2>&1 | tail -2

echo "=== target file BEFORE ==="
if [ ! -e "$FILE" ]; then
  echo "MISSING: $FILE   (path prefix differs on this box? try the /murrlab or /data prefix)"
  exit 3
fi
stat -c 'owner=%U(%u) group=%G(%g) mode=%a  %n' "$FILE"

echo "=== attempt: chgrp ${TARGET_GID} then chmod g+rw (the real fix, additive) ==="
if chgrp "$TARGET_GID" -- "$FILE"; then echo "chgrp: OK"; else echo "chgrp: FAIL ($?)"; fi
if chmod g+rw -- "$FILE"; then echo "chmod: OK"; else echo "chmod: FAIL ($?)"; fi

echo "=== target file AFTER ==="
stat -c 'owner=%U(%u) group=%G(%g) mode=%a  %n' "$FILE"

echo "=== verdict ==="
g=$(stat -c '%g' "$FILE")
m=$(stat -c '%a' "$FILE")
if [ "$g" = "$TARGET_GID" ] && [ $(( 0$m & 020 )) -ne 0 ]; then
  echo "SUCCESS: file is now gid ${TARGET_GID} and group-writable — alcatraz CAN fix uid $(id -u)'s files."
else
  echo "NO-GO: still gid=$g mode=$m — this uid isn't a server-side member of gid ${TARGET_GID}."
fi
