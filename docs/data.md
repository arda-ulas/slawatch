# Synthetic data: what the generator produces and why

> **Everything described here is synthetic.** The customers, sites, services, outages and
> tickets are produced by `src/slawatch/generate.py` from a seed. No real company, network or
> ticket system was used. The domain is a *telecom enterprise service desk*: a business-customer
> account team wants to catch SLA breaches on trouble tickets early.

The generator is deterministic: the same `--seed`, `--n-tickets`, `--start` and `--end` always
produce byte-identical CSVs. All numeric parameters live in `src/slawatch/config.py`; this
document explains them.

```
uv run slawatch-generate --seed 20240701 --n-tickets 200000 --start 2024-07-01 --end 2026-07-01 --out data/raw
```

## Files written to `data/raw/`

| File | Rows (default run) | What it is |
|---|---|---|
| `synthetic_customers.csv` | 40 | Enterprise customers: `customer_id, customer_name, industry, tier, hq_province` |
| `synthetic_sites.csv` | 444 | Customer sites: `site_id, customer_id, site_name, city, province, region, timezone` |
| `synthetic_services.csv` | 885 | Service instances at sites: `service_id, site_id, customer_id, service_type, bandwidth_mbps, assignment_group` |
| `synthetic_sla_targets.csv` | 16 | Contracted resolution target hours per `tier x severity` |
| `synthetic_outage_incidents.csv` | 8 | Injected regional outages: `outage_id, region, service_type, start_date, end_date, cause` |
| `synthetic_tickets.csv` | 200,600 (200,000 + 600 duplicates) | The **dirty** raw ticket extract (see "Injected dirtiness") |
| `synthetic_ticket_status_history.csv` | ~870k | One row per status change: `ticket_id, status, change_date, change_reason` |
| `synthetic_generation_metadata.json` | – | Seed, parameters, row counts, breach rate, exact dirtiness counts, `"synthetic": true` |

Every file name carries the `synthetic_` prefix and the metadata file carries an explicit
disclaimer so the origin is unambiguous wherever the files travel.

## TMF621 mapping

The ticket shape follows the TM Forum **TMF621 Trouble Ticket** resource, flattened to
snake_case columns. Nested TMF sub-resources are flattened as follows:

| TMF621 field | Column(s) | Notes |
|---|---|---|
| `id` | `id` | `TT-0000001` …, assigned in creation order |
| `name` | `name` | Short symptom title |
| `description` | `description` | Templated sentence; optional (blank in ~5 % of raw rows) |
| `ticketType` | `ticket_type` | `incident`, `service_request`, `query`, `complaint` |
| `severity` | `severity` | `critical`, `major`, `minor`, `low` |
| `priority` | `priority` | 1–4; derived from severity, nudged by tier |
| `status` | `status` | TMF states in snake_case: `acknowledged`, `in_progress`, `pending`, `held`, `resolved`, `closed`, `cancelled` (`held` is allowed by the schema but not generated) |
| `channel.name` | `channel` | `phone`, `email`, `web_portal`, `chat`, `api`, `monitoring` |
| `creationDate` | `creation_date` | UTC |
| `lastUpdate` | `last_update` | Timestamp of the latest status change |
| `expectedResolutionDate` | `expected_resolution_date` | `creation_date + SLA target hours` (the SLA due time) |
| `requestedResolutionDate` | `requested_resolution_date` | Customer-requested; present on ~30 % of tickets |
| `resolutionDate` | `resolution_date` | Final resolution; NULL while open or if cancelled |
| `relatedParty[role=customer].id` | `customer_id` | → `synthetic_customers.csv` |
| `relatedEntity[@referredType=GeographicSite].id` | `site_id` | → `synthetic_sites.csv` |
| `relatedEntity[@referredType=Service].id` | `service_id` | → `synthetic_services.csv` |
| `relatedParty[role=assignedGroup].name` | `assignment_group` | Technician group the service routes to |
| `relatedEntity[@referredType=Outage].id` | `active_outage_id` | Outage active for the ticket's region + service type at creation, if any |
| *(extension)* | `open_backlog_at_creation` | Tickets open in the assignment group at the creation instant |
| *(derived from `statusChangeHistory`)* | `reopen_count` | Number of `resolved → in_progress` transitions |
| `statusChangeHistory[]` | separate file `synthetic_ticket_status_history.csv` | `status`, `changeDate → change_date`, `changeReason → change_reason` |

Not modelled: `href`, `externalId`, `attachment`, `note`, `relatedParty` beyond customer and
group, and the TMF `@type`/`@baseType` polymorphism fields.

## Dimensions

**Customers (40).** Tier drawn with weights platinum 15 %, gold 30 %, silver 35 %, bronze 20 %.
Industry uniform over eight sectors (financial services, healthcare, retail, manufacturing,
public sector, logistics, education, energy). Names are built from a word list plus an
industry-flavoured suffix ("Beacon Pipelines", "Cedar Marketplaces"). Each customer has a
hidden `ops_latent ~ N(0, 0.18)` that shifts its resolution times — a well-run customer
resolves faster; this is what makes `customer_id` informative to a model without being
deterministic.

**Sites (444).** Per customer: platinum 15–30, gold 8–18, silver 4–10, bronze 2–5 sites. Half
the sites land in the HQ province, the rest anywhere, with city weights favouring Toronto,
Montreal, Vancouver, Calgary and Edmonton. Provinces roll up to five operating regions:
`ontario`, `quebec`, `west` (BC, AB), `prairies` (SK, MB), `atlantic` (NS, NB, NL, PE). Each
site has a hidden volume weight `LogNormal(0, 0.5)`.

**Services (885).** Each site subscribes to 1–4 service instances (35/35/20/10 %) drawn without
replacement from `dedicated_internet` 30 %, `sd_wan` 20 %, `business_voice_sip` 20 %,
`managed_cloud` 10 %, `managed_security` 10 %, `mpls_wan` 10 %. Relative ticket rate per instance:
sd_wan 1.3, voice 1.1, internet 1.0, cloud 0.9, mpls 0.8, security 0.7. Access services
(internet, SD-WAN, MPLS) route to the regional field group `field_<region>`; voice, cloud and
security route to national `voice_ops`, `cloud_ops`, `security_ops`.

**SLA targets (hours).**

| tier | critical | major | minor | low |
|---|---|---|---|---|
| platinum | 4 | 8 | 24 | 72 |
| gold | 6 | 12 | 48 | 96 |
| silver | 8 | 24 | 72 | 120 |
| bronze | 12 | 36 | 96 | 168 |

## Arrival process

Baseline tickets follow a non-homogeneous process on an hourly grid over the window. Hourly
intensity is the product of:

* **Weekday profile** (Mon→Sun): 1.18, 1.12, 1.06, 1.02, 0.95, 0.42, 0.36.
* **Hour-of-day profile** in America/Toronto local time, peaking 09:00–11:00 and 13:00–15:00,
  with a night floor around 0.16–0.22 of the daytime rate.
* **Month profile**: December 0.85, January 1.05, February 1.02, July 0.92, August 0.93.
* **Growth**: +8 % per year.

`n_tickets` minus the outage allocation is spread over hours by a multinomial draw on the
normalised intensity, then jittered uniformly within the hour. Each ticket is assigned to a
service instance in proportion to its volume weight.

**Categoricals.** Ticket type: incident 72 %, service request 18 %, query 7 %, complaint 3 %.
Severity for incidents: critical 7 %, major 23 %, minor 45 %, low 25 %; for non-incidents:
1/9/45/45 %. Channel: phone 30 %, email 22 %, web portal 20 %, chat 8 %, api 5 %, monitoring
15 % (monitoring only for incidents). Priority = severity rank, moved one step up for platinum
with p = 0.5 and one step down for bronze with p = 0.3.

## Outage incidents

Eight regional incidents are injected at fixed day offsets from `--start` (they are dropped if
they fall outside a shortened window). Each has a region, a service type, a duration (6–48 h),
a cause label, and a *share* of `n_tickets` (0.3–0.8 %) that is added as extra tickets whose
creation times are front-loaded within the window (`Beta(1.3, 3)`), whose severity skews to
critical 35 % / major 45 %, and whose channel skews to phone 40 % / monitoring 35 %.

`active_outage_id` is set on **any** ticket (outage-driven or baseline) created while an
outage was active for its region + service type. This is the creation-time view of "there is
a known major incident right now".

## Resolution-time model (where the label comes from)

For every ticket, time-to-first-resolution in hours is log-normal around the SLA target:

```
log(hours) = log(target_hours) + BASE_LOG_OFFSET (-1.30)
           + severity effect     critical -0.20, major -0.05, minor +0.05, low +0.15
           + tier effect         platinum -0.12, gold -0.04, silver +0.04, bronze +0.10
           + service effect      internet 0, sd_wan +0.18, voice +0.05, cloud -0.05, security -0.15, mpls +0.10
           + channel effect      phone 0, email +0.22, web_portal +0.08, chat +0.05, api -0.05, monitoring -0.25
           + ticket-type effect  incident 0, service_request +0.10, query -0.30, complaint +0.15
           + 0.28 * after_hours + 0.35 * weekend         (site-local time; business hours 08–18)
           + 0.45 * outage_active
           + 0.40 * clip(backlog / capacity - 1, 0, 1.5)  (backlog pressure, see below)
           + 0.55 * pending                                 (waits on the customer; p = 0.14, 0.30 for service requests)
           + customer latent  N(0, 0.18)
           + group latent     N(0, 0.10)
           + noise            N(0, 0.62)
```

The ticket **breaches** when final resolution time exceeds the target. With the defaults this
gives a breach rate of about 15 % among resolved tickets (15.1 % on the default 200k run;
16.4 % on a 40k run). The signal is learnable from creation-time information (severity, tier,
service, channel, time of day/week, outage flag, backlog) but far from deterministic: the
pending flag, the customer/group latents and the noise term are all invisible at creation.

**Backlog feedback.** Tickets are simulated in creation order. For each ticket the generator
counts the tickets still open in its assignment group (`open_backlog_at_creation`) and applies
the backlog term above. Capacity per group is calibrated inside the run as 1.3 × the 75th
percentile of the backlog observed in a first pass with the feedback switched off, so the
effect is size-invariant and the queue cannot run away; the effect saturates at
`ratio − 1 = 1.5`.

## Lifecycle and status history

* **Cancelled** with p = 0.015: history is `acknowledged → cancelled`; no resolution date.
* Otherwise `acknowledged` (creation) → `in_progress` (after an acknowledgement delay:
  median 2 min for monitoring, ~20 min otherwise, ×2.5 after hours/weekends) → optional
  `pending` / `in_progress` pair → `resolved` → `closed` 24–72 h later.
* **Reopened** with p = 0.04 for non-cancelled incidents: after first resolution, `in_progress`
  again 2–72 h later, then a second `resolved`. Final `resolution_date` is the second one;
  `reopen_count` = 1. The SLA outcome is evaluated on the *final* resolution, which is why
  reopened tickets breach far more often (a deliberate simplification; a first-resolution SLA
  would be a one-line change).
* **Snapshot.** `--end` is the "as of" moment. Status changes after it are not emitted, tickets
  whose end falls after it are open (`acknowledged` / `in_progress` / `pending`), and their
  `resolution_date` is NULL. `last_update` is the last emitted change.

Not modelled: SLA clock pauses while `pending`, `held`, multiple reopen cycles, and
priority changes over the ticket's life.

## Injected dirtiness (raw ticket extract only)

Applied by `apply_dirtiness` after the clean frames are built, so tests can compare the raw
extract against the truth. Exact counts for the run are recorded in the metadata JSON under
`dirtiness_injected`.

| Defect | Fraction of rows | Details |
|---|---|---|
| Local-offset timestamps | 5 % of `creation_date`, 5 % of `resolution_date` | `2025-03-04T13:22:10-05:00` (America/Toronto) instead of `…18:22:10Z` |
| Naive timestamps | 2 % of `creation_date`, 2 % of `resolution_date` | `2025-03-04 18:22:10` with no zone (it is UTC) |
| Impossible rows | 0.08 % | `resolution_date` 1–48 h *before* `creation_date` |
| Casing variants | 3 % | `ticket_type`, `severity`, `status`, `channel`, `assignment_group` upper-cased or Title Cased with spaces (`Web Portal`) |
| Whitespace | 2 % | The same columns padded with spaces / tabs |
| Missing description | 5 % | Empty string |
| Missing priority | 1 % | Empty string |
| Exact duplicate rows | 0.3 % | Appended copies of existing rows |

Timestamps in the *dimension* files and the status-history file are clean ISO-8601 UTC.
The pipeline (`src/slawatch/cleaning.py`) normalises categoricals, drops duplicates, parses all
timestamp variants to UTC, imputes priority from severity, and rejects impossible rows into
`data/processed/synthetic_tickets_rejected.csv` with a reason; every count is logged and stored
in `load_run.cleaning_report`.

## Creation-time-safe columns (no leakage)

A classifier predicting `sla_breached` may use only information available when the ticket is
opened. `src/slawatch/features.py` encodes this split:

**Safe at creation time** (`CREATION_TIME_FEATURES`): `ticket_type`, `severity`, `priority`,
`channel`, `customer_id`, `tier`, `industry`, `site_id`, `region`, `province`, `service_id`,
`service_type`, `assignment_group`, `active_outage_id`, `open_backlog_at_creation`,
`sla_target_hours`, `creation_hour_local`, `creation_dow_local`, `is_weekend`,
`is_after_hours`, `has_requested_resolution_date`. Also safe: `creation_date`,
`expected_resolution_date`, `requested_resolution_date`, `name`, `description`.

**Outcomes — never features** (`OUTCOME_FIELDS`): `status`, `last_update`, `resolution_date`,
`reopen_count`, `resolution_hours`, `is_resolved`, `sla_breached`, and anything from the
status-history table.

`sla_breached` semantics in the processed data and the `fact_ticket` table: `true`/`false` for
resolved tickets (final resolution vs. target); `true` for open tickets already past their
expected resolution date at the snapshot; `NULL` for open tickets still inside their window and
for cancelled tickets. Model training should use resolved tickets only.
