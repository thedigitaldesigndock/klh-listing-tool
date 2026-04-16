# KLH Listing Tool — Windows install runbook

Peter's runbook for rolling the tool out to Kim's and Nicky's PCs.
Same steps on each machine. Budget ~20 minutes per machine.

---

## Prerequisites (install these once per machine)

1. **Python 3.11+** — download from <https://www.python.org/downloads/>.
   On the installer's first screen, **tick "Add python.exe to PATH"**
   before clicking Install Now.

2. **Git for Windows** — <https://git-scm.com/download/win>. Accept all
   defaults. This gives us `git` on PATH for the auto-pull-on-launch.

   Kim and Nicky each have their own local `ONE/` and `TWO/` scan folders
   on their desktop and list independently — no shared inbox. `setup.bat`
   creates the folders; they just drop scans in. No Google Drive needed
   for the listing workflow.

---

## Install steps

On the target PC:

```cmd
cd C:\
mkdir KLH
cd KLH
git clone https://github.com/<your-user>/klh-listing-tool.git
cd klh-listing-tool
install\setup.bat
```

`setup.bat` will:
- create the Python venv
- `pip install` all dependencies
- write `C:\Users\<user>\.klh\config.yaml` (template with Windows paths)
- write `C:\Users\<user>\.klh\.env` (empty — you fill it in next)
- create `C:\KLH\data\` with the working subdirs
- create `%USERPROFILE%\Desktop\ONE` and `%USERPROFILE%\Desktop\TWO`
  as the local scan-drop folders

When it finishes it'll tell you to fill in the `.env` and copy tokens.

> The config template assumes the Windows username is **Kim**. If it's
> something different, open `%USERPROFILE%\.klh\config.yaml` in Notepad
> and change `C:/Users/Kim/Desktop/ONE` and `.../TWO` to match the
> actual username.

---

## Wire up the eBay credentials

Two files need to come from your master Mac:

1. **`~/.klh/.env`** — has `EBAY_APP_ID`, `EBAY_CERT_ID`, `EBAY_DEV_ID`.
   Open `C:\Users\<user>\.klh\.env` in Notepad on the target PC and
   paste the three values in.

2. **`~/.klh/tokens.json`** — has the OAuth refresh token.
   Copy this file verbatim to `C:\Users\<user>\.klh\tokens.json` on
   the target PC. Do **not** regenerate it per-machine — the same
   refresh token works everywhere and only one machine can be the
   "latest refresher" at a time.

> The quickest transfer is: zip both files into a password-protected
> archive, send to yourself via email, download + extract on the
> target PC, delete the archive. Don't put these in Google Drive.

---

## Desktop shortcut

Right-click `C:\KLH\klh-listing-tool\install\launch.bat` → **Send to →
Desktop (create shortcut)**. Rename the shortcut to **KLH Listing
Tool**. Double-click to launch.

Every launch will:
1. `git pull` the latest version from your Mac's pushes
2. start the dashboard on `http://localhost:8765`
3. open the browser

Close the cmd window to stop the server.

---

## Verify it works

1. Double-click the shortcut. Browser opens to the dashboard.
2. Drop a scanned photo into `%USERPROFILE%\Desktop\ONE\` (and a card
   into `TWO\` if that listing type needs one).
3. Hit **Scan ONE / TWO** in the dashboard — the file should show up
   as a row.
4. Pick any product, render the mockup, step through compose → list
   (against sandbox if you set `EBAY_ENV=sandbox` in `.env`).

If `git pull` fails because Nicky/Kim can't auth to GitHub — the
repo must be public, or you must configure a GitHub PAT in Git
Credential Manager. For a two-machine rollout, public repo is
simpler.

---

## Pushing updates (from your Mac)

Normal workflow:

```bash
cd /Volumes/Samsung_990_4TB/KLH/klh-listing-tool
# make changes, run tests, commit
git push
```

Next time Kim or Nicky double-click the shortcut, `launch.bat`'s
`git pull` picks up your commit. No rebuilds, no installer re-runs.

Rare exception — if you change `pyproject.toml` (add a new Python
dependency), tell them to close the dashboard and run
`install\setup.bat` once more to pick up the new package. That's the
only time re-setup is needed.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python is not recognized` | Python not on PATH. Re-run the Python installer, tick the PATH box. |
| `git is not recognized` | Install Git for Windows. |
| Browser opens to blank page | Server didn't bind in time. Hit refresh after 5 seconds. |
| `config not found` on launch | `%USERPROFILE%\.klh\config.yaml` missing — rerun `setup.bat`. |
| Dashboard says "picture_dir missing" | Desktop has a non-standard name (OneDrive-redirected, different username, etc.). Open `%USERPROFILE%\.klh\config.yaml` in Notepad and fix the `picture_dir` / `card_dir` paths. |
| `token refresh failed` | `tokens.json` is stale. Copy fresh copy from your Mac. |
