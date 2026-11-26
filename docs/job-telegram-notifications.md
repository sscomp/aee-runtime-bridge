# MCP Job terminal notifications through Hermes

The MCP Gateway can send a Telegram notification after a sealed Job becomes
`completed`, `failed`, `timed_out`, or `cancelled`. This covers every executor
adapter and is separate from the legacy Task Dispatcher notifier. Terminal
status, rather than CLI exit code, determines the notification's outcome.

The optional background worker starts after the gateway binds successfully.
It checks the persistent Job store every two seconds, without changing Job
records, success criteria, dispatch leases, MCP responses or executor outcomes.
Notification delivery is independent of Job success. No LLM is invoked.

## Operator configuration

Create an operator-owned 0600 file at
`~/.config/aee/job-notifications.json`, using
[the disabled example](../config/examples/job-notifications.json). Set:

- `enabled`: `true` to opt in.
- `hermes_bin`: absolute path to the installed Hermes CLI.
- `hermes_home`: the existing Hermes profile used by the Telegram gateway.
- `target`: `telegram` for its home chat, or `telegram:<chat-id>[:thread-id]`.
- `enabled_at`: fixed timezone-aware ISO timestamp for the first eligible
  **finish time**. Keep it unchanged across restarts. Older Jobs are not sent.

`AEE_JOB_NOTIFICATION_CONFIG` optionally selects another config path. Restart
the AEE gateway when changing configuration, after confirming no Job is active.
Deleting the config or setting `enabled:false` disables the worker on restart.
Disabling notifications does not stop Jobs or delete delivery receipts.

The notifier calls `hermes send --to <target> --file - --json`, using the
profile's existing gateway platform adapter and credentials. For Telegram,
Hermes' bot-token delivery path does not require starting another agent or
polling gateway. No separate bot token is copied into AEE. Child processes
receive only basic shell/locale variables and the configured `HERMES_HOME`;
executor/model credentials and Telegram overrides are not inherited.

The message includes Job ID, node, agent, terminal status, fixed-format error
code, exit code and finish/duration timestamps. It excludes the task, result
summary, raw errors, logs, working directory and execution metadata. Detailed
results remain available through the existing AEE Job tools.

## Delivery evidence and failure handling

Receipts live in `<job-store>/notifications/<job-id>.json`, with directory mode
0700 and file mode 0600. A numeric Hermes/Telegram `message_id` plus
`success:true` is required for `state:sent`; a skipped send is not delivery.
The outbox is protected by a cross-process lock and durable atomic writes.
Restarting the gateway preserves sent receipts and skips duplicate sends.

Only a failure to start the Hermes process is eligible for retries. The
notifier makes at most three attempts, with delays of 60 and 300 seconds.
A nonzero CLI exit, 30-second transport timeout, I/O error, missing or
unusable acknowledgement, or restart during `sending` is recorded as
`unknown` and stops automatic retries. On read, the notifier also marks older
failed receipts as `unknown` when they lack a confirmed launch failure. Check
the Telegram conversation before retrying an uncertain send. The notification
has a delivery status independent of the Job's terminal status.

The shared `aee._notification_guard` suppresses both the poller and transport
in verification contexts. Automated tests use temporary stores and mocked
transport; they must never deliver real notifications. One separately
authorized live smoke can verify the configured Hermes route after rollout.

Useful log events are `job_notifications_enabled`, `job_notification`,
`job_notification_record_rejected`, `job_notification_poll_failed`, and
`job_notifications_configuration_failed`. Logs never include CLI stdout,
stderr, recipient details, tokens or message bodies.
