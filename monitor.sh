#!/bin/bash
# Hourly backfill snapshot -> monitor.log (detached companion to backfill.py).
# Usage: nohup ./monitor.sh > /dev/null 2>&1 &
cd "$(dirname "$0")"
echo "$(date '+%F %T') monitor started (hourly snapshots)" >> monitor.log
while true; do
  sleep 3600
  {
    echo "===== $(date '+%F %T') ====="
    if [ -f backfill.pid ] && ps -p "$(cat backfill.pid)" > /dev/null 2>&1; then
      echo "backfill: RUNNING pid=$(cat backfill.pid)"
    else
      echo "backfill: NOT RUNNING (finished or dead — check backfill.log tail)"
    fi
    python3 -c "
import json, sqlite3, os
try:
    p = json.load(open('progress.json'))
    from collections import Counter
    total = len(p)
    try:
        db = os.path.expanduser(os.environ.get('OPENCODE_DB', '~/.local/share/opencode/opencode.db'))
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        n = con.execute('SELECT COUNT(*) FROM session').fetchone()[0]
        con.close()
        total = f'{len(p)}/{n}'
    except Exception:
        pass
    print('progress:', dict(Counter(v['status'] for v in p.values())), 'total:', total)
except Exception as e:
    print('progress: unreadable:', e)
" 2>&1
    api="${COGNEE_API_URL:-http://localhost:8010}"
    curl -s --max-time 15 "$api/api/v1/datasets" 2>/dev/null | \
      python3 -c "import json,sys; print('datasets:', [d['name'] for d in json.load(sys.stdin)])" 2>&1
    curl -s --max-time 10 "$api/health" 2>/dev/null | head -c 80; echo
    tail -n 3 backfill.log 2>/dev/null
  } >> monitor.log 2>&1
done
