# DCI Data Lab

Flask and SQLite analytics site served at `https://awertt.org/dci/`.

The expanded database covers the 2000-2019 and 2022-2026 DCI seasons. Historical Division I is normalized to World Class. Historical Division II and Division III are normalized to Open Class, while the original source division remains represented by the official performance and recap payloads used to build the database.

## Pages

- `/dci/` season dashboard
- `/dci/corps/Colts` corps history
- `/dci/head-to-head?a=Colts&b=Troopers` direct comparison with wins, losses, and ties
- `/dci/records` exact ties, closest non-tied spreads, highs, and jumps
- `/dci/query` easy filters
- `/dci/sql` password-protected read-only SQL console

## Expand an installed server to 2000

Run as root:

```bash
curl -fsSL https://raw.githubusercontent.com/awertt/awertt.org/main/apps/dci-lab/update-history.sh | bash
```

This pulls the current application, deploys the tie-aware pages, collects official CompetitionSuite records for 2000-2014, merges them with the existing raw archive, rebuilds the SQLite database, validates every required season, backs up the previous database, and restarts the service.

The process is resumable. A validated `dci-raw-2000-2014.zip` is reused on later runs.

## Application-only update

```bash
cd /opt/awertt-dci/repo
git fetch origin
git reset --hard origin/main
/opt/awertt-dci/venv/bin/pip install -r apps/dci-lab/requirements.txt
systemctl restart awertt-dci
```

The live database is stored outside the Git repository at `/var/lib/awertt-dci/dci_scores_master.sqlite`. Timestamped backups are created before historical database replacement.
