# AEE job quicklook

`scripts/aee-job.sh` queries jobs through an **existing, configured local
`aee-bridge`**. It supports status, result and polling without dispatching work.
The bridge is a separate prerequisite, not installed by this helper. It owns
transport and authentication; never place credentials in helper arguments or
source a credential file in a shell.

## Optional manual installation

Requires Bash 3.2 or newer, `jq` on PATH and an executable bridge. From the root
of a reviewed checkout, choose a durable source location and install both
command names if desired:

```sh
mkdir -p "$HOME/.local/bin"
install -m 755 scripts/aee-job.sh "$HOME/.local/bin/aee-job"
ln -s aee-job "$HOME/.local/bin/aee-job.sh"
```

These commands are an explicit installation step, not part of gateway startup.
Check for existing commands first and back them up; do not overwrite an existing
installation or alias without a reviewed replacement and rollback plan. Ensure
`$HOME/.local/bin` is on PATH. Updating the checkout alone does not update an
installed copy or an alias that points elsewhere.

By default, the helper executes `$HOME/.local/bin/aee-bridge`. Override it with
`AEE_BRIDGE_BIN=/absolute/path/to/configured-bridge`. This is an executable path,
not a gateway URL or a shell command. The bridge must accept exactly
`job-status JOB_ID` and `job-result JOB_ID`, returning a JSON object with boolean
`ok`, matching `job_id`, and a lifecycle `status`. Existing result errors and
summaries pass through in JSON output. The helper does not configure a gateway,
choose a gateway from the prefix, or read credentials itself.

## Lookup and polling

```sh
JOB_ID='M2JOB-20261117-0123456789abcdef0123456789abcdef'
aee-job "$JOB_ID"                     # same as status
aee-job status "$JOB_ID" --json
aee-job result "$JOB_ID"
aee-job watch "$JOB_ID" --interval 5 --json
```

IDs match the shared `aee/mcp_runtime/job_identity.py` reader grammar:

```text
[A-Z][A-Z0-9]{0,15}JOB-[0-9]{8}-[0-9a-f]{32}
```

The node code is 1–16 uppercase ASCII letters/digits, starting with a letter,
followed by literal `JOB`. A3JOB, N2JOB, M2JOB and future valid prefixes are
accepted unchanged. Generation uses a UTC date; readers check exactly eight
ASCII date digits without calendar validation, followed by exactly 32 lowercase
hex digits. Uppercase hex, whitespace, Unicode lookalikes, paths and shell
metacharacters are rejected locally with exit 2 before calling the bridge.
The validator uses a local C locale without changing the bridge's locale.

`watch` accepts integer intervals of 1–3600 seconds (default 5), polls while
queued/running, then fetches the terminal result. Ctrl+C stops polling with exit
130; the dispatched job continues. JSON mode emits one object per line:
`{"kind":"status","data":{...}}` or `{"kind":"result","data":{...}}`.
Single status lookups do not fetch results. Text output retains the original
Chinese presentation.

To cancel, use the existing bridge's `cancel "$JOB_ID"` operation and check
status afterward; this read-only helper does not cancel jobs.

| Exit | Meaning |
| --- | --- |
| 0 | Lookup succeeded, including failed/cancelled jobs or JOB_NOT_COMPLETE results |
| 2 | Invalid arguments, job ID or polling interval |
| 3 | JOB_NOT_FOUND from the configured gateway |
| 4 | Missing dependency, bridge failure, or invalid gateway response |
| 130 | Polling interrupted with Ctrl+C |

Inspect `.data.status`, `.data.ok` and `.data.error_code` to determine job success;
helper exit 0 alone does not mean the job succeeded. Re-run after a transient
connection failure. Keep the original ID and query its originating gateway.

## Offline verification

```sh
bash -n scripts/aee-job.sh
shellcheck scripts/aee-job.sh
PYTHONPATH=.:tests/mcp python3 -B -m unittest test_job_helper -v
```

Tests use a temporary logging bridge and synthetic responses only, comparing
shell acceptance with the shared Python validator. They do not contact a gateway
or need credentials. The Bash source uses 3.2-compatible syntax; Linux tests with
`BASH_COMPAT=3.2` are not a native macOS or actual Bash 3.2 runtime qualification.
