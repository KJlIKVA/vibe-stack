#!/usr/bin/env bash
# Состояние пайплайна (SQLite) живёт в ветке `state`: один коммит без истории, чтобы бинарный
# файл не раздувал репозиторий. Резервная копия каждого запуска — в артефактах workflow.
#
#   scripts/state.sh pull          — скачать state/vibe_stack.db из ветки state
#   scripts/state.sh push [метка]  — сохранить state/vibe_stack.db в ветку state
#
# Защиты: pull отличает «ветки ещё нет» от сетевой ошибки (при ошибке — стоп, никакого пустого
# состояния); push сохраняет только после успешного pull, только целую БД, не меньше прежней,
# и только если ветку никто не изменил за это время (--force-with-lease).
set -euo pipefail
DB=state/vibe_stack.db
MARK=state/.pulled        # SHA скачанного коммита или «none», если ветки не было
SEEN=state/.pulled_seen   # сколько строк было в seen при скачивании
# STATE_REMOTE — для локальной проверки скрипта на bare-репозитории
REMOTE="${STATE_REMOTE:-https://x-access-token:${GITHUB_TOKEN:?нужен GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY:?нужен GITHUB_REPOSITORY}.git}"

# печатает число строк в seen; падает, если БД битая
check_db() {
  python3 - "$1" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    sys.exit("state: БД повреждена")
tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print(db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] if "seen" in tables else 0)
PY
}

case "${1:-}" in
  pull)
    mkdir -p state
    rm -f "$MARK" "$SEEN" "$DB.tmp"
    set +e
    git ls-remote --exit-code --heads "$REMOTE" state >/dev/null 2>&1
    rc=$?
    set -e
    if [ "$rc" -eq 2 ]; then
      echo "state: ветки state ещё нет — начинаем с пустого состояния"
      echo none > "$MARK"
      echo 0 > "$SEEN"
      exit 0
    elif [ "$rc" -ne 0 ]; then
      echo "state: не удалось связаться с репозиторием (код $rc) — останавливаюсь, чтобы не работать вслепую" >&2
      exit 1
    fi
    git fetch -q --depth=1 "$REMOTE" state
    sha=$(git rev-parse FETCH_HEAD)
    git show FETCH_HEAD:vibe_stack.db > "$DB.tmp"
    check_db "$DB.tmp" > "$SEEN"
    mv "$DB.tmp" "$DB"
    echo "$sha" > "$MARK"
    echo "state: загружен коммит ${sha:0:7}, строк seen: $(cat "$SEEN")"
    ;;
  push)
    if [ ! -f "$MARK" ]; then
      echo "state: pull не выполнен или не удался — сохранять нельзя" >&2
      exit 1
    fi
    if [ ! -f "$DB" ]; then echo "state: нечего сохранять"; exit 0; fi
    seen_now=$(check_db "$DB")
    seen_before=$(cat "$SEEN")
    if [ "$seen_now" -lt "$seen_before" ]; then
      echo "state: строк seen стало меньше ($seen_before → $seen_now) — это не похоже на нормальный запуск, не сохраняю" >&2
      exit 1
    fi
    expected=$(cat "$MARK")
    [ "$expected" = none ] && expected=""
    tmp=$(mktemp -d)
    cp "$DB" "$tmp/vibe_stack.db"
    printf 'Служебная ветка: SQLite-состояние пайплайна Vibe Stack. Пишет только GitHub Actions.\n' > "$tmp/README.md"
    git -C "$tmp" init -q -b state
    git -C "$tmp" add -A
    git -C "$tmp" -c user.name="vibe-stack-bot" -c user.email="vibe-stack-bot@users.noreply.github.com" \
      commit -q -m "state: run ${GITHUB_RUN_ID:-local} ${2:-}"
    for attempt in 1 2 3; do
      if git -C "$tmp" push -q "--force-with-lease=state:$expected" "$REMOTE" state; then
        echo "state: сохранено (seen: $seen_now)"
        exit 0
      fi
      echo "state: push не удался (попытка $attempt)" >&2
      sleep $((attempt * 5))
    done
    echo "state: не удалось сохранить состояние — ветку изменил кто-то ещё или нет связи" >&2
    exit 1
    ;;
  *)
    echo "использование: $0 pull|push [метка]" >&2
    exit 2
    ;;
esac
