# DCI Data Lab

Flask and SQLite analytics site served at `https://awertt.org/dci/`.

## Pages

- `/dci/` season dashboard
- `/dci/corps/Colts` corps history
- `/dci/head-to-head?a=Colts&b=Troopers` direct comparison
- `/dci/records` closest spreads, highs, and jumps
- `/dci/query` easy filters
- `/dci/sql` password-protected read-only SQL console

## Server install

Run as root:

```bash
curl -fsSL https://raw.githubusercontent.com/awertt/awertt.org/main/apps/dci-lab/install-server.sh | bash
```

The installer creates a restricted `dciweb` service account, downloads the SQLite database from Drive, validates it, installs a systemd service on port 3001, and adds an Nginx `/dci/` route without replacing the existing site on port 3000.

## Update application code

```bash
cd /opt/awertt-dci/repo
git fetch origin
git reset --hard origin/main
/opt/awertt-dci/venv/bin/pip install -r apps/dci-lab/requirements.txt
systemctl restart awertt-dci
```

The database is stored outside the Git repository at `/var/lib/awertt-dci/dci_scores_master.sqlite`.
