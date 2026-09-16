# Findings from the analytical views (synthetic data)

> **All numbers below come from synthetic data** produced by `slawatch-generate` with the
> default parameters (`seed 20240701`, 200,000 tickets, 2024-07-01 → 2026-07-01) and loaded by
> `slawatch-pipeline` on 2026-09-16. Several "findings" are simply the generator's own
> assumptions showing through; each item says so where that is the case. See
> [`data.md`](data.md) for the model that produced the data.

## The run

| Step | Result |
|---|---|
| `make data` | 200,000 tickets, 870,359 status changes, 444 sites, 885 services, 8 outages; generator 5.0 s, 10.8 s wall including CSV writes |
| `make load` | 200,600 raw rows → 199,840 `fact_ticket` rows (600 exact duplicates dropped, 160 impossible rows rejected, 2,015 priorities imputed, 10,094 rows with categorical case/whitespace fixes, 13,960 mixed-format timestamps parsed); 869,667 history rows (692 orphan rows of rejected tickets dropped); 22.1 s to load and build views, 38.3 s wall including cleaning and the Tableau extract |
| Snapshot | 2026-07-01 00:00 UTC: 196,387 resolved, 2,975 cancelled, 478 open (55 already past due) |
| Headline | **15.10 % of resolved tickets breached their SLA** (29,654 of 196,387) |
| View latency | every view answers in well under a second on this volume (`v_backlog_ageing_monthly`, the heaviest, 0.42 s; the others 1–130 ms) |

## 1. Outages are 4 % of tickets but set the shape of the monthly compliance curve

`v_outage_impact` and `v_outage_vs_normal`. Tickets opened while an outage was active for their
region and service type number 8,541 (4.3 % of the book) yet breach at **54.6 %** versus
**13.3 %** for everything else — roughly 4,660 breaches, about one in six of all breaches.
Ticket volume during the eight incidents ran 45× to 1,038× the 28-day baseline for the same
region + service type (the 1,038× is the prairies MPLS cut, whose baseline is only 0.03
tickets/hour). Critical + major severity share during outages is ~78–83 % versus ~25 % normally.

The monthly compliance series shows exactly which months contain an outage: August 2024
(22.1 % breach), October 2024 (20.9 %), January 2025 (18.8 %), June 2025 (17.3 %), December
2025 (19.9 %) and April 2026 (17.6 %), against a 12.6–13.5 % floor in quiet months.

*Generator note:* the outage flag adds +0.45 to log-resolution-time and outage tickets are
drawn with high severity, so both the breach uplift and the severity mix are built in. What is
not built in is the *size* of the monthly swing; that comes out of the interaction between
outage volume and the backlog feedback. The time-based membership computed in SQL matches the
generator's `active_outage_id` flag exactly (8,541 = 8,541), which is a useful check that the
view logic is right.

## 2. Nights and weekends are the biggest single risk factor

Breach rate among resolved tickets by site-local creation time:

| weekend | after hours (outside 08–18) | tickets | breach |
|---|---|---|---|
| no | no | 118,664 | **10.3 %** |
| no | yes | 49,991 | 19.3 % |
| yes | no | 18,551 | 22.6 % |
| yes | yes | 9,181 | **38.9 %** |

A weekend-night ticket is almost four times as likely to breach as a business-hours one. Both
flags are known at creation time, so they are cheap, strong features for the classifier.

*Generator note:* effects of +0.28 (after hours) and +0.35 (weekend) are additive in
log-space, which is why the combination is worse than either alone. The magnitude of the
resulting breach gap is a consequence of the noise scale, not something set directly.

## 3. Backlog pressure only bites at the top of the distribution

`open_backlog_at_creation` split into quintiles among resolved tickets: 11.7 %, 13.8 %, 14.9 %,
13.3 %, **21.8 %** breach for the lowest to highest quintile. The first four quintiles
(0–82 open tickets in the group) are nearly flat; the top quintile (82–1,142) is where the
breach rate jumps. Per group, the two field groups with the highest breach rates are
`field_west` (20.9 %, average backlog 81) and `field_atlantic` (20.1 %, average backlog 41),
while `security_ops` sits at 7.7 % with a maximum backlog of 50.

*Generator note:* this is the saturating hinge in the model (`0.40 × clip(backlog/capacity − 1,
0, 1.5)`) showing through — a linear feature would under-fit this; a tree model or a
"backlog above group-typical" feature would catch it. The per-group ordering is a mix of the
group latent, the regional service mix (SD-WAN routes to field groups and has the slowest
service effect) and the two large outages that hit `field_west` and `field_atlantic`.

## 4. Bronze customers breach the most despite the loosest targets

`v_resolution_stats` and the tier breakdown: bronze tickets breach at **21.9 %** with an average
target of 100 h and a median resolution of 49 h; platinum, gold and silver sit at 14.5–14.8 %.
The SLA multiplier alone does not save a low-tier account.

*Generator note:* almost entirely built in — bronze carries a +0.10 tier effect and a 30 %
chance of a one-step priority downgrade, modelling a desk that de-prioritises small accounts.
The interesting part is that the per-tier breach rates for the other three tiers are so
close: the SLA table was scaled so that the *target* absorbs most of the severity/tier
difference, and the data confirms it.

## 5. Email is the slow channel; monitoring and API are fast

Breach by channel: email **18.9 %** (median 22.2 h), web portal 14.9 %, phone 14.5 %, chat
14.3 %, monitoring **11.9 %** (median 11.6 h), API 11.2 %. Service type spreads similarly:
SD-WAN 19.3 %, MPLS 15.4 %, voice 15.2 %, dedicated internet 14.8 %, managed cloud 11.4 %,
managed security **7.7 %**.

*Generator note:* both are direct reads of the channel and service effects (email +0.22,
monitoring −0.25; sd_wan +0.18, managed_security −0.15). The ordering is exactly what was
configured; the value is that a model should recover it.

## 6. Reopened tickets and "pending" waits are post-hoc explanations, not predictors

Tickets that were reopened breach at **79.9 %** (5,834 tickets) versus 13.1 % for the rest;
tickets that went through a `pending` (waiting on customer) state breach at **30.6 %** (32,967
tickets) versus 12.0 %. Both are enormous effects — and both are only known after the ticket
has progressed, which is why `reopen_count` and the status history are listed under
`OUTCOME_FIELDS` in `features.py` and must stay out of the classifier.

*Generator note:* the reopen figure is inflated by a modelling choice — the SLA is evaluated on
the final resolution, so a reopen adds 2–72 h of gap plus more work on top of a ticket that had
already consumed most of its window. A first-resolution SLA would shrink this a lot.

## 7. Ten accounts to watch, and the trend column matters more than the rank

`v_top_customers_at_risk` for May 2026 (the latest complete month) puts two bronze energy and
retail accounts at the top with 3-month rolling breach rates of 39.5 % and 30.4 %, but both
are `improving` month over month (−3.3 pp and −21.4 pp). The only `worsening` account in the
top ten is a bronze healthcare customer (21.7 % rolling, +3.9 pp). Three of the ten are gold or
platinum accounts with 850–1,230 tickets in the window — an 18–20 % rolling breach rate there
represents far more breached tickets than the bronze accounts at the top of the list.

*Generator note:* the tier skew is built in (finding 4); the per-customer ordering within a tier
is the hidden `ops_latent` plus noise. Month-over-month changes for small accounts (79 tickets
over three months) are mostly sampling noise, which is why the view requires at least 20
outcome-bearing tickets in the window and shows the count.

## 8. The open backlog is short-lived, and the ageing snapshot is sensitive to when you look

`v_backlog_ageing_current` at the snapshot: 478 open tickets, of which 272 are under 24 h old,
139 are 1–3 days, 45 are 3–7 days, 21 are 7–30 days and 1 is over 30 days; 55 are already past
due, almost all of them in the 3-day-plus buckets. `v_backlog_ageing_monthly` shows the
first-of-month backlog ranging from 224 (September 2025) to **600 (November 2024, 153 past
due)** — the latter is four days after the 30-hour Quebec voice outage.

*Generator note:* the tail is short because nothing in the model produces multi-week tickets
except the reopen gap and the top of the log-normal; a real desk would have a fatter 30d+
bucket (parked changes, third-party dependencies). The monthly view samples the 1st of each
month; `f_backlog_ageing(timestamptz)` can be called for any instant.

## What is *not* a finding

* Weekday/weekend volume (Tue–Fri 32–37k tickets each, Sat/Sun 13–15k in Toronto time) and the
  8 %/year growth are the arrival profile, verbatim.
* The 15.1 % headline breach rate was tuned by choosing `BASE_LOG_OFFSET`; it is a design
  target, not an observation.
* Service-request tickets breach more than incidents (18.5 % vs 15.1 %) and queries far less
  (5.8 %) because of the ticket-type effects and the pending probability for service requests.
