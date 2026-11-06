# LXC and OCI creation wizard — working proposal

This branch is for early design and implementation review. It is not a release
candidate, a completed Roadmap item, or a request to merge the whole proposal.
Feedback on the scope, user flow and integration is welcome before continuing.

## What to review

Open **VMs & LXCs → Create LXC** in the existing Monitor inventory. The main
navigation and the existing application-management pages are unchanged.

- **Plain Linux container:** five compact groups for system identity, access,
  resources, network/DNS and final review. Creation uses native Proxmox APIs.
- **OCI application:** browse the maintained catalog, inspect an application's
  details, choose recommended or advanced settings, then answer the existing
  engine's questions. The catalog remains the owner of application metadata.
  Recipes, choices, defaults, validation and installation stay in the original
  engine; the web layer transports and displays them.
- **Custom image or Compose:** the same original engine handles the input.
- **Community Scripts:** catalog, details and preparation only. Web installation
  is unfinished and would be a separate follow-up after feedback on LXC/OCI.

Closing the dialog keeps an ongoing preparation available. Explicit cancellation
ends that preparation; the next attempt starts fresh. Failed or uncertain jobs
are not automatically replayed. Creating resources requires a separate final
confirmation in the existing engine or the native LXC flow.

The creation API exposes only the catalog/custom installation operations from
the shared OCI adapter. It does not expose the broader manager's update, remove,
registry-management or recovery actions through this creation surface. Older
standalone OCI lifecycle prototypes and their private profiles are omitted.

## Local checks

Use the Python/Node versions and dependencies in the repository's current CI
workflows. The qualified lanes use Python 3.11 and Node 22.14.0; the full OCI
engine job uses Python 3.13. Archive fixtures also need `zstd`.

From the repository root, with the CI Python dependencies installed:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts/tests -v
python3 -I -B .github/scripts/run_offline_qualified.py --lane python
```

The Node lane has its own small locked dependency set:

```sh
npm ci --prefix .github/ci/offline-node --legacy-peer-deps --ignore-scripts --no-audit --no-fund
```

In a clean checkout without `AppImage/node_modules`, use the same fixture setup
as CI:

```sh
ln -s ../.github/ci/offline-node/node_modules AppImage/node_modules
NODE_PATH="$PWD/AppImage/node_modules" python3 -I -B .github/scripts/run_offline_qualified.py --lane node
```

Use a separate checkout with the full `AppImage/package-lock.json` dependencies
to build the frontend:

```sh
cd AppImage
npm ci --legacy-peer-deps --ignore-scripts --no-audit --no-fund
npm run export
```

Optional real-browser fixtures are `tests/test_native_lxc_browser.cjs`,
`tests/test_create_unified_browser.cjs` and
`tests/test_create_cancelled_browser.cjs`. They use the actual React components
with intercepted API requests. They require an installed `playwright-core`
module and Chromium. `PLAYWRIGHT_CORE` and `CHROMIUM` can select those tools;
the fixtures never contact a real Proxmox server or create a guest.

## Controlled live testing

Use an isolated test node. This preview is not an automatic deployment tool.
The matching maintained OCI engine, including the web transport modules, must
be installed coherently at the normal ProxMenux engine location. Do not mix
individual transport files with an older engine. Browsing never downloads,
repairs or upgrades the engine automatically; missing components fail closed.

Native LXC creation is disabled until an administrator explicitly configures
the machine-bound activation policy, allowed ID ranges and protected resources.
Policy administration is a local root action, not a browser permission or a
request-body flag. Private pilot activation formats are not accepted by this
proposal. Existing journals remain readable; they are not replayed.

The adapters currently use the Monitor's full-admin JWT authentication. OIDC
and user-profile integration need coordination with the work already proposed
for the Monitor. This is one reason the navigation redesign is kept separate.

## Verification limits

Selected single-node creation scenarios were exercised during earlier local
development. That is not full lifecycle coverage of every catalog application,
storage type or host configuration, and live creation has not been repeated
for this separated export.

Real HA failover, migration and multi-node behavior are unverified. Automated
admission guards are covered; they are not evidence of working failover. A
dedicated multi-node HA test environment is still needed for those scenarios.
Inline template downloads and hardware passthrough are also outside the
currently enabled native creation surface.

The upstream repository already has TypeScript diagnostics; static export
skips type checking under the existing Next.js configuration. This proposal
adds no diagnostics in the recorded comparison, but does not claim a clean
repository-wide TypeScript check.

On the development environment, three unchanged mapped-image import tests fail
because the filesystem denies the FICLONE ioctl with EPERM. The same failures
were reproduced on the unmodified develop baseline. Other platform-dependent
engine tests require PHP or a root-owned NVIDIA hook. These results must not be
reported as a fully green engine suite or successful live image imports.

Please review or test only the scenarios available in your environment, and
suggest what should change before expanding the proposal or adding Community
Scripts installation.
