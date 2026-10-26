# Monitoring

How to tell the service is doing its job, and when someone is needed: what is watched, the targets, the rules that call for someone, and what to do when each one fires. Everything is worked out from the log the service already writes ([logging](logging.md)), by [scripts/monitor.py](../scripts/monitor.py). Tests hold every rule to the events and fields the service really writes, and this page to the rules.

## Why from the log

The usual way is a `/metrics` endpoint that a Prometheus server scrapes. Here, every event is already a JSON line with its counts and timings, so the same signals come from the log with nothing added to the service:

- **No new route to protect.** Metrics say how much each route is used and how often access is refused; on an open endpoint that is something to hide, and behind a login it is one more thing to get right.
- **Nothing new to keep from leaking.** The log is already held to holding no document text and no question, by tests ([logging](logging.md#what-is-never-written)). A metric would be a second place for a label to carry something it shouldn't.
- **The rules carry over.** Each is a count, a share or a 95th percentile of one event in a window, which any log store's alerting runs as it stands (an example [below](#in-production)).

What it costs: the numbers exist only once the log is read, a few minutes late, not continuously. For one service on one machine that is enough; at the scale where it isn't, the log store does the counting ([below](#in-production)).

## Running it

```
docker compose logs --no-log-prefix --since 15m api | python -m scripts.monitor
python -m scripts.monitor api.log --window 60
```

It reads the last 15 minutes before the newest line, or `--window` minutes, and the thresholds below are set for 15. The exit code says who is needed:

| Exit code | Means |
|---|---|
| 0 | Nothing needs anyone |
| 1 | Something to look at today: a ticket |
| 2 | Something to act on now: a page |
| 3 | No log lines to read |

Every 5 minutes from cron, with whatever sends a message to someone (`notify` here stands for that):

```
*/5 * * * * cd /srv/knowledge && docker compose logs --no-log-prefix --since 15m api | python -m scripts.monitor > /var/log/knowledge-monitor.txt; [ $? -eq 2 ] && notify page < /var/log/knowledge-monitor.txt
```

A line that doesn't parse, or is missing a field a rule reads, from an older version of the service say, is left out of that rule rather than stopping the report; lines that aren't JSON are counted, and are a rule of their own.

## What it reports

Run on the Docker stack after a mix of use and misuse: four people searching ten times each, 22 wrong passwords across three accounts, David opening a restricted document 12 times and an admin page 12 times, and three questions, refused because the stack has no model:

```
window: 2026-10-26 12:49:07 to 12:49:35 UTC, 0.5 minutes, 401 lines

requests:
  POST /search                             40 requests     0 5xx  p50 43 ms  p95 57 ms
  POST /auth/login                         26 requests     0 5xx  p50 86 ms  p95 91 ms
  GET /documents/{document_id}             12 requests     0 5xx  p50 24 ms  p95 27 ms
  GET /admin/documents/{document_id}       12 requests     0 5xx  p50 24 ms  p95 48 ms
  POST /ask                                 3 requests     3 5xx  p50 5 ms  p95 6 ms
  GET /documents                            1 requests     0 5xx  p50 8 ms  p95 8 ms
  audit: highest record sealed 108; python -m scripts.audit check should show it too

signals:
  ok     access-check-disagreed                    0  (fires at 1) times an access check disagreed with the other
  ok     audit-not-written                         0  (fires at 1) audit records that couldn't be written
  ok     server-errors                          0.0%  (fires at 5.0%) of requests failed with an unexpected error
  ok     unexpected-errors                         0  (fires at 1) unexpected errors
  ok     retrieval-slow                        15 ms  (fires at 500 ms) p95 of retrieval
  ok     answers-slow               too few to judge  (fires at 5000 ms) p95 of an answered question
  ok     model-failing                             0  (fires at 10) failed calls to the model
  ok     no-information-high        too few to judge  (fires at 50.0%) of questions got no information
  ok     links-removed                             0  (fires at 1) answers that had a link to somewhere else taken out
  ok     index-behind                              0  (fires at 1) signs the index is behind the database
  TICKET logins-failing                           22  (fires at 20) failed logins
  ok     requests-limited                          0  (fires at 10) requests over a person's limit
  TICKET documents-probed                         12  (fires at 10) documents asked for by someone who may not read them
  TICKET actions-denied                           12  (fires at 10) actions refused for want of a permission
  ok     log-not-json                              0  (fires at 1) lines that weren't JSON

3 firing, worst: ticket
  ticket: docs/monitoring.md#logins-failing
  ticket: docs/monitoring.md#documents-probed
  ticket: docs/monitoring.md#actions-denied
exit 1
```

The three questions show as `5xx` in the requests: a 503 is what `/ask` gives when asking is switched off or the model is down. They aren't server errors, which are 500s, the ones nothing handled.

The first run of this missed the admin pages: the script asked for `GET /admin/documents`, a route that doesn't exist, which is a 405 and checks no permission. The monitor was right, the traffic wasn't.

## The signals

The four usual ones, and the ones this service adds:

| Signal | From | Watched by |
|---|---|---|
| Traffic | `request handled`, by route | the requests table |
| Errors | `request handled` with 500, `unexpected error` | `server-errors`, `unexpected-errors` |
| Latency | `chunks retrieved` and `request handled` for `/ask`, against NFR-1 | `retrieval-slow`, `answers-slow` |
| Saturation | `request limited`, and the model failing under its quota | `requests-limited`, `model-failing` |
| The access checks | the two checks of every chunk disagreeing | `access-check-disagreed` |
| The audit trail | records not written, and the highest written | `audit-not-written`, the audit line |
| Misuse | wrong passwords, restricted documents and actions asked for | `logins-failing`, `documents-probed`, `actions-denied` |
| Answer quality | "no information" answers, and links an answer tried to carry | `no-information-high`, `links-removed` |
| Freshness | documents not yet indexed | `index-behind` |
| Cost | tokens sent to the model and given back (NFR-3) | the model line |
| The log itself | lines that aren't JSON | `log-not-json` |

Whether the containers are up is Docker's health check ([Docker](docker.md#health-checks)): `docker compose ps`. A container that is down writes nothing, so the monitor would say "no log lines to read" (exit 3), which a cron job should treat as a page too.

## Targets

The latency targets are NFR-1's, the same numbers the evaluation holds the service to ([evaluation report](evaluation-report.md)), and a test keeps them the same:

- **Retrieval:** p95 at most 500 ms. Measured in the evaluation: 7.4 ms; on the stack above, 15 ms.
- **An answered question:** p95 at most 5 s, with Groq. Measured in the evaluation: 1.6 s.

There is no availability target. One container on one machine has nothing to fail over to, so a number would be a promise the design can't keep; `server-errors` pages on a share of failures instead.

The other thresholds are starting points, not measurements: there is no real traffic to tune them on. Each is set to stay quiet through what the evaluation and the tests do, and to fire on what the examples above do.

## The rules

Every rule is a count, a share or a 95th percentile of one or more events in the window. A share or a percentile needs enough lines to mean something, and says "too few to judge" until then.

| Rule | Severity | Fires at | Reads |
|---|---|---|---|
| `access-check-disagreed` | Page | 1 | `search returned chunks outside the scope`, `vector search returned chunks outside the allowed list`, `second check removed chunks the scope allowed` |
| `audit-not-written` | Page | 1 | `audit record not written` |
| `server-errors` | Page | 5% of at least 20 requests | `request handled` |
| `unexpected-errors` | Ticket | 1 | `unexpected error` |
| `retrieval-slow` | Ticket | 500 ms p95 of at least 20 | `chunks retrieved` |
| `answers-slow` | Ticket | 5000 ms p95 of at least 20 | `request handled` |
| `model-failing` | Ticket | 10 | `model call failed` |
| `no-information-high` | Ticket | 50% of at least 20 questions | `question answered` |
| `links-removed` | Ticket | 1 | `question answered` |
| `index-behind` | Ticket | 1 | `the vector index is behind the database; rebuild it with: python -m scripts.index build`, `search done` |
| `logins-failing` | Ticket | 20 | `login failed` |
| `requests-limited` | Ticket | 10 | `request limited` |
| `documents-probed` | Ticket | 10 | `document not returned` |
| `actions-denied` | Ticket | 10 | `action denied` |
| `log-not-json` | Ticket | 1 | lines that aren't JSON |

**Pages** are for what the service promises: that nobody reads what they may not, and that everything is audited. One of those failing even once is an incident. **Tickets** are for what someone should look at today.

## What to do

Each rule's entry, which the report links to. Every log line has a `request_id`; `docker compose logs --no-log-prefix api | grep <id>` gives the rest of that request. The audit trail, read by a security reviewer, says who it was and what they asked ([audit](audit.md)).

### access-check-disagreed

The scope query and the second check of each chunk gave different answers, or the vector search returned a chunk it wasn't allowed to. Nothing leaked: the request failed or the chunk was dropped. But one of the two checks is wrong, and the next difference might be one they both get wrong. Treat it as a security incident: find the request IDs, find in the audit trail who asked and which documents were involved, and look at what changed (a deployment, a grant, a role). Don't restart to make it go away. Decision record [0003](adr/0003-pre-filter-vector-search.md).

### audit-not-written

A request was refused because its audit record couldn't be stored, so the service is refusing work rather than doing it unaudited ([0006](adr/0006-fail-request-when-audit-fails.md)). Usually the database: the disk is full, the volume is read-only, or the file is locked. `docker compose exec api df -h /data`, and the `error` field says the kind of failure. Then run `python -m scripts.audit check` once it is back.

### server-errors

At least 5% of requests failed with an error nothing handled. `unexpected-errors` will be firing too: its lines have the error's type and where it happened. Look at what changed last; roll back if it was a deployment.

### unexpected-errors

An error nothing handled, logged with its type and the traceback without its message ([logging](logging.md#every-event)). One may be a bug in a rare path: open a ticket with the type, the `where` and the request ID. Many is `server-errors`.

### retrieval-slow

Retrieval's p95 is over 500 ms. `chunks retrieved` has the whole retrieval and `rerank_milliseconds`; `search done` and `access scope decided` have their own. A reranker switched on is the usual cause ([0012](adr/0012-reranking-off-by-default.md)); a person who reads very many documents is the other, the permission filter's cost grows with what they may read ([evaluation](evaluation.md#at-a-larger-size)).

### answers-slow

Answered questions take more than 5 s at p95. `model answered` has the model's `milliseconds` and `attempts`: retries mean the provider is struggling, which `model-failing` watches too. Otherwise compare `question answered`'s `retrieval_milliseconds` with `generation_milliseconds` to see which side it is.

### model-failing

Ten calls to the model failed in 15 minutes. The `error` field says how: a timeout, the provider down, or its rate limit. Groq's free tier allows about three answers a minute ([generation](generation.md)). Search still works without the model (NFR-5). Check the provider's status page, and the quota if it is the rate limit.

### no-information-high

At least half of the questions got "no information". Either people are asking about what isn't there, or something stopped matching: the index behind (`index-behind`), the embedding model changed, or the relevance threshold wrong for the questions being asked. `chunks retrieved` has `best_score` and `below_threshold` for each; compare them with the evaluation ([retrieval](retrieval.md)).

### links-removed

An answer had a link to somewhere other than its sources, and it was taken out before it was shown ([generation](generation.md)). The model put it there, most likely because a document told it to: a prompt injection in a document someone uploaded. Find the request ID's audit record for the documents used, and read them.

### index-behind

Documents that people may read aren't in the index, so they can't be found yet: the service said so at startup, or a search found some. `python -m scripts.index status` says whether it is up to date (exit code 2 when it isn't), and `python -m scripts.index build` publishes a new version.

### logins-failing

Twenty failed logins in 15 minutes: someone forgetting a password, or someone guessing. Each line has the `reason` and, when the email belongs to someone, the `user_id`; the login throttle stops each email after five ([API](api.md#too-many-failed-logins)). Many accounts at once is guessing. The audit trail has every attempt.

### requests-limited

People are being refused for going over their limit of questions or searches ([API](api.md#how-often)). One person many times is a script or a stuck client: the `user_id` says who. Many people means the limits are too low for how the service is used.

### documents-probed

Ten requests for documents that exist but the person may not read. They got the same 404 as for a document that doesn't exist, so they learnt nothing ([0005](adr/0005-same-response-for-no-access-and-not-found.md)). One person working through IDs is looking for something; the `user_id` and the audit trail say who and which. Asking for documents that don't exist doesn't count.

### actions-denied

Ten actions refused for want of a permission: someone trying the admin pages, or a client offering a button it shouldn't. The `permission` field says which; the `user_id` says who.

### log-not-json

The service writes nothing but JSON lines ([logging](logging.md#the-format)), so something else is writing to its output: a library's progress bar or print, or the server's own lines if logging wasn't set up. Find the lines with `docker compose logs --no-log-prefix api | grep -v '^{'`. Nothing here leaks by itself, but whatever is writing is outside the rules that keep content out of the log.

## Also run

- **`python -m scripts.audit check` once a day.** It checks every audit record against its seal and says the highest ID; the report's audit line gives the highest the log has seen. A trail that ends before the log's does has had records deleted from its end, seals and all ([0013](adr/0013-seal-each-audit-record.md)). Exit code 2 when something doesn't match.
- **`docker compose ps`**, or the same from cron: the health checks.
- **`python -m scripts.index status`**, exit code 2 when the index is behind the database.
- **`python -m scripts.scan_secrets`** before each deployment ([secrets](secrets.md)).

## In production

The same rules, in a log store's alerting. With Loki, and the API's log labelled `service="api"` when it is collected, `logins-failing` is:

```
sum(count_over_time({service="api"} | json | message="login failed" [15m])) >= 20
```

and `retrieval-slow`:

```
quantile_over_time(0.95, {service="api"} | json | message="chunks retrieved" | unwrap milliseconds [15m]) >= 500
```

The thresholds and the runbook stay the same; only the counting moves. Not set up here.

## Not covered

- **The UI** isn't watched beyond its health check. Its errors are the API's errors, which are.
- **The machine**: disk, memory and CPU are the host's monitoring. A full disk shows here as `audit-not-written`.
- **Notifying anyone.** The exit code says who is needed; sending the message is left to whatever the machine already uses.
- **Trends.** Each run judges one window; nothing remembers the last one, so a slow climb shows only once it crosses a threshold.
