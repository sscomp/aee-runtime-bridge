# AEE Bootstrap v1 — Offline / Air-gapped Bundle Guide

> **Audience:** Operators deploying AEE on air-gapped or
> network-restricted hosts.
> **Spec reference:** `reports/aee_bootstrap_v1_spec.md` §7.5 (Offline /
> Air-gapped), §9.3 (Clone / Update Behavior), §6.3 (Package Managers)

---

## 1. Overview

The offline bundle is a self-contained archive that allows bootstrapping
AEE on hosts without outbound network access. It contains:

1. **Git repository** — a full clone of the AEE repo at a specific ref.
2. **Dependency manifests** — apt/brew/winget package lists.
3. **Python lock file** — `python.requirements.lock` for `uv pip install`.
4. **Python packages** — a wheelhouse of pre-downloaded Python packages
   (optional but recommended for fully air-gapped hosts).

---

## 2. Building the Bundle

### 2.1 On a machine with network access

```bash
# 1. Clone the repo at the desired ref
git clone --filter=blob:none <remote> /tmp/aee-bundle/repo
git -C /tmp/aee-bundle/repo checkout <ref>
git -C /tmp/aee-bundle/repo submodule update --init

# 2. Copy dependency manifests
cp -r /path/to/repo/bootstrap/manifests /tmp/aee-bundle/manifests

# 3. Build the Python wheelhouse (optional, for fully air-gapped)
cd /tmp/aee-bundle/repo
uv pip install -r requirements.lock --target /tmp/aee-bundle/wheelhouse

# 4. Create the archive
tar czf aee-offline-bundle-$(date +%Y%m%d).tar.gz -C /tmp/aee-bundle .
```

### 2.2 Bundle contents

```
aee-offline-bundle-YYYYMMDD.tar.gz
├── repo/                  # full git clone at the pinned ref
│   ├── .git/
│   ├── aee/
│   ├── bootstrap/
│   │   ├── manifests/
│   │   │   ├── apt.deps.txt
│   │   │   ├── brew.deps.txt
│   │   │   ├── pwsh.deps.txt
│   │   │   ├── python.requirements.in
│   │   │   └── python.requirements.lock
│   │   └── lib/
│   ├── install.sh
│   └── install.ps1
├── manifests/             # top-level copy for shell-layer access
└── wheelhouse/             # pre-downloaded Python packages (optional)
```

---

## 3. Using the Bundle

### 3.1 Transfer to the air-gapped host

```bash
scp aee-offline-bundle-YYYYMMDD.tar.gz operator@airgapped-host:/tmp/
```

Or via a physical medium (USB, DVD).

### 3.2 Install on the air-gapped host

```bash
# 1. Extract the bundle
mkdir -p /opt/aee-bundle
tar xzf /tmp/aee-offline-bundle-YYYYMMDD.tar.gz -C /opt/aee-bundle

# 2. Run the bootstrap with --offline
aee install \
  --repo-root /opt/aee-bundle/repo \
  --profile mini \
  --offline \
  --offline-bundle /opt/aee-bundle \
  --yes
```

### 3.3 Windows (experimental)

```powershell
# Extract
Expand-Archive aee-offline-bundle-YYYYMMDD.zip -DestinationPath C:\aee-bundle

# Run
.\C:\aee-bundle\repo\install.ps1 -RepoRoot C:\aee-bundle\repo `
  -Profile mini -Offline -OfflineBundle C:\aee-bundle -Yes
```

---

## 4. Offline Doctor

```bash
aee doctor --offline --offline-bundle /opt/aee-bundle
```

In offline mode:
- H4 (Network) checks that the bundle is complete (repo, manifests, lock
  file) instead of testing outbound reachability.
- H6 (Python) checks `uv pip list` against the bundle's lock file.
- Other checks run as normal.

---

## 5. Offline Update

```bash
aee update --channel stable --offline --offline-bundle /opt/aee-bundle-new
```

The `--offline-bundle` points to a NEW bundle containing the updated
ref. The bootstrap:
1. Verifies the new bundle's `commit_sha` matches the requested channel.
2. Copies the new repo content over the existing install.
3. Re-runs `uv pip install` against the new lock file.
4. Re-pins the `03_pin` marker.

> Profile switch is still rejected even in offline mode (§2.4).

---

## 6. Partial Offline

If the host has access to a local PyPI mirror or a local git remote but
not the public internet:

```bash
# Point pip at the local mirror
export PIP_INDEX_URL=http://local-mirror:8080/simple/
export UV_INDEX_URL=http://local-mirror:8080/simple/

# Point git at the local remote
git -C /path/to/repo remote set-url origin http://local-git/aee.git

# Run normally (no --offline needed)
aee install --profile mini
```

---

## 7. Validation

After an offline install, verify:

```bash
# 1. Agent is ready
cat /path/to/repo/bootstrap/stages/07_agent_ready
# Should show state: "completed"

# 2. Doctor passes
aee doctor --offline --offline-bundle /opt/aee-bundle
# Should exit 0

# 3. Pin matches the bundle
cat /path/to/repo/bootstrap/stages/03_pin
# commit_sha should match the bundle's git ref
```

---

## 8. Known Limitations

1. **No auto-download of missing dependencies.** If a required apt/brew
   package is not installed and not in the bundle, the bootstrap fails
   with `EXIT_DEPENDENCY_FLOOR_NOT_MET` (code 12). Pre-install all
   system dependencies before running the offline bootstrap.

2. **Wheelhouse may be platform-specific.** A wheelhouse built on
   Ubuntu 22.04 will not work on macOS. Build separate bundles per
   platform.

3. **Git submodules must be included.** If the repo uses submodules,
   ensure `git submodule update --init` was run before archiving.

4. **Lock file must match the ref.** The `python.requirements.lock`
   in the bundle must match the pinned `commit_sha`. If they diverge,
   `aee doctor` reports drift (exit 9).

---

_End of offline bundle guide._