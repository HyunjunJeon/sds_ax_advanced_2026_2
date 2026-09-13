# Privacy-preserving telemetry

GJC includes a minimal update and adoption telemetry path. It is **disabled by default** and sends nothing unless enabled in the user configuration.

## What can be sent

Events use schema version `1` and a fixed allowlist. The current event names are:

- `update_check_started`
- `update_check_completed`
- `update_install_started`
- `update_install_completed`
- `update_install_failed`

An event may contain only:

- the schema version;
- the fixed event name;
- a locally generated random UUIDv4 install ID;
- an ISO timestamp;
- the stable/nightly update channel;
- a fixed result (`available`, `up_to_date`, `installed`, `failed`, or `skipped`); and
- a fixed update method (`bun`, `npm`, `binary`, or `migrate`).

Unknown fields are discarded. Values outside the allowlist are rejected before transport.

## What is never collected

Telemetry never serializes prompts, command-line arguments, paths, environment variables, secrets, account identity, model or provider identity, repository identity, arbitrary error text, hostname, username, machine ID, or IP-derived identity. The install ID is generated randomly and is not derived from device, network, account, or repository data.

The install ID is stored locally in the user agent directory as `telemetry-install-id` with private file permissions. A malformed existing ID fails closed rather than being replaced.

## Controls and kill switch

Enable or disable the feature with the normal settings surface:

```sh
gjc config set telemetry.enabled true
gjc config set telemetry.enabled false
gjc config get telemetry.enabled
```

The setting defaults to `false`. The emergency process-level kill switch always wins over the setting:

```sh
GJC_DISABLE_TELEMETRY=1 gjc update
```

The kill switch accepts `1`, `true`, `yes`, or `on`. It is evaluated before an event is scheduled.

## Transport behavior

Events are sent as HTTPS `POST` requests to `https://telemetry.gajae.dev/v1/events`. Transport is best effort: calls are scheduled without blocking the update command, at most two requests are in flight, and each request is bounded by a 1500 ms timeout. Disabled telemetry, queue saturation, offline failures, timeouts, non-success responses, and serializer failures do not affect GJC behavior and are not printed as arbitrary error text.

No local event queue or transcript is retained. Server-side retention and aggregate reporting are outside the client contract and must be documented by the service before production collection is enabled.
