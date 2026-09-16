"""Synthetic trouble-ticket generator for a telecom enterprise service desk.

All output is synthetic. The ticket shape follows the TM Forum TMF621 Trouble Ticket resource,
flattened to snake_case columns; docs/data.md documents the mapping, every distribution used
here, and the dirtiness deliberately injected into the raw extract.

Usage:
    uv run slawatch-generate --seed 42 --n-tickets 200000 --out data/raw
"""

from __future__ import annotations

import argparse
import heapq
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from slawatch import __version__
from slawatch import config as C

log = logging.getLogger("slawatch.generate")

TICKET_COLUMNS = [
    "id",
    "name",
    "description",
    "ticket_type",
    "severity",
    "priority",
    "status",
    "channel",
    "creation_date",
    "last_update",
    "expected_resolution_date",
    "requested_resolution_date",
    "resolution_date",
    "customer_id",
    "site_id",
    "service_id",
    "assignment_group",
    "active_outage_id",
    "open_backlog_at_creation",
    "reopen_count",
]
HISTORY_COLUMNS = ["ticket_id", "status", "change_date", "change_reason"]
TIMESTAMP_COLUMNS = [
    "creation_date",
    "last_update",
    "expected_resolution_date",
    "requested_resolution_date",
    "resolution_date",
]
CATEGORICAL_COLUMNS = ["ticket_type", "severity", "status", "channel", "assignment_group"]

FILE_NAMES = {
    "customers": "synthetic_customers.csv",
    "sites": "synthetic_sites.csv",
    "services": "synthetic_services.csv",
    "sla_targets": "synthetic_sla_targets.csv",
    "outages": "synthetic_outage_incidents.csv",
    "tickets": "synthetic_tickets.csv",
    "status_history": "synthetic_ticket_status_history.csv",
    "metadata": "synthetic_generation_metadata.json",
}


@dataclass(frozen=True)
class GenerationParams:
    seed: int = C.DEFAULT_SEED
    n_tickets: int = C.DEFAULT_N_TICKETS
    start: str = C.DEFAULT_START
    end: str = C.DEFAULT_END

    @property
    def start_ts(self) -> pd.Timestamp:
        return pd.Timestamp(self.start, tz="UTC")

    @property
    def end_ts(self) -> pd.Timestamp:
        return pd.Timestamp(self.end, tz="UTC")


@dataclass
class GeneratedData:
    """Clean (pre-dirtiness) frames plus the dirtied raw ticket extract."""

    customers: pd.DataFrame
    sites: pd.DataFrame
    services: pd.DataFrame
    sla_targets: pd.DataFrame
    outages: pd.DataFrame
    tickets: pd.DataFrame  # clean, typed
    status_history: pd.DataFrame
    raw_tickets: pd.DataFrame  # dirtied, all-string extract that gets written to disk
    metadata: dict = field(default_factory=dict)


# ======================================================================================
# Dimensions
# ======================================================================================
def _weighted_choice(rng: np.random.Generator, options: list, weights: dict, size: int):
    p = np.array([weights[o] for o in options], dtype=float)
    p /= p.sum()
    return rng.choice(np.array(options, dtype=object), size=size, p=p)


def build_customers(rng: np.random.Generator, n_customers: int = 40) -> pd.DataFrame:
    names = rng.permutation(C.CUSTOMER_NAME_PARTS_A)[:n_customers]
    tiers = _weighted_choice(rng, C.TIERS, C.TIER_WEIGHTS, n_customers)
    industries = rng.choice(np.array(C.INDUSTRIES, dtype=object), size=n_customers)
    city_weights = np.array([c[3] for c in C.CITIES], dtype=float)
    hq_idx = rng.choice(len(C.CITIES), size=n_customers, p=city_weights / city_weights.sum())
    rows = []
    for i in range(n_customers):
        industry = industries[i]
        suffixes = C.INDUSTRY_NAME_SUFFIX[industry]
        suffix = suffixes[rng.integers(len(suffixes))]
        rows.append(
            {
                "customer_id": f"CUST-{i + 1:03d}",
                "customer_name": f"{names[i]} {suffix}",
                "industry": industry,
                "tier": tiers[i],
                "hq_province": C.CITIES[hq_idx[i]][1],
                # hidden latent: negative = well-run customer whose tickets resolve faster
                "hidden_ops_latent": float(rng.normal(0.0, C.CUSTOMER_LATENT_SD)),
            }
        )
    return pd.DataFrame(rows)


def build_sites(rng: np.random.Generator, customers: pd.DataFrame) -> pd.DataFrame:
    site_ranges = {"platinum": (15, 30), "gold": (8, 18), "silver": (4, 10), "bronze": (2, 5)}
    city_weights = np.array([c[3] for c in C.CITIES], dtype=float)
    rows = []
    for cust in customers.itertuples(index=False):
        lo, hi = site_ranges[cust.tier]
        n_sites = int(rng.integers(lo, hi + 1))
        same_prov = [i for i, c in enumerate(C.CITIES) if c[1] == cust.hq_province]
        sp_w = city_weights[same_prov] / city_weights[same_prov].sum()
        for k in range(n_sites):
            if rng.random() < 0.5:
                ci = same_prov[rng.choice(len(same_prov), p=sp_w)]
            else:
                ci = int(rng.choice(len(C.CITIES), p=city_weights / city_weights.sum()))
            city, prov, tz, _ = C.CITIES[ci]
            short = cust.customer_name.split()[0]
            rows.append(
                {
                    "site_id": None,
                    "customer_id": cust.customer_id,
                    "site_name": f"{short} {city} #{k + 1}",
                    "city": city,
                    "province": prov,
                    "region": C.PROVINCE_TO_REGION[prov],
                    "timezone": tz,
                    "hidden_volume_weight": float(rng.lognormal(0.0, 0.5)),
                }
            )
    sites = pd.DataFrame(rows)
    sites["site_id"] = [f"SITE-{i + 1:04d}" for i in range(len(sites))]
    return sites


def build_services(rng: np.random.Generator, sites: pd.DataFrame) -> pd.DataFrame:
    n_services_p = np.array([0.35, 0.35, 0.20, 0.10])
    types = np.array(C.SERVICE_TYPES, dtype=object)
    type_p = np.array([C.SERVICE_TYPE_WEIGHTS[t] for t in C.SERVICE_TYPES])
    type_p /= type_p.sum()
    rows = []
    for site in sites.itertuples(index=False):
        n = int(rng.choice([1, 2, 3, 4], p=n_services_p))
        chosen = rng.choice(types, size=n, replace=False, p=type_p)
        for st in chosen:
            has_bw = st in ("dedicated_internet", "sd_wan", "mpls_wan")
            bw = int(rng.choice(C.BANDWIDTH_OPTIONS_MBPS)) if has_bw else None
            assignment = C.SERVICE_ASSIGNMENT[st]
            group = f"field_{site.region}" if assignment == "regional" else assignment
            rows.append(
                {
                    "service_id": None,
                    "site_id": site.site_id,
                    "customer_id": site.customer_id,
                    "service_type": st,
                    "bandwidth_mbps": bw,
                    "assignment_group": group,
                    "hidden_volume_weight": site.hidden_volume_weight * C.SERVICE_TICKET_RATE[st],
                }
            )
    services = pd.DataFrame(rows)
    services["service_id"] = [f"SVC-{i + 1:05d}" for i in range(len(services))]
    services["bandwidth_mbps"] = services["bandwidth_mbps"].astype("Int64")
    return services


def build_sla_targets() -> pd.DataFrame:
    rows = [
        {"tier": tier, "severity": sev, "target_hours": float(hours)}
        for tier, sevs in C.SLA_TARGET_HOURS.items()
        for sev, hours in sevs.items()
    ]
    return pd.DataFrame(rows)


def build_outages(params: GenerationParams) -> pd.DataFrame:
    rows = []
    window_hours = (params.end_ts - params.start_ts).total_seconds() / 3600
    for i, o in enumerate(C.OUTAGE_INCIDENTS):
        start_h = o["day"] * 24 + 9  # incidents start around 09:00 UTC-ish
        if start_h + o["hours"] > window_hours:
            continue  # outside a shortened window
        rows.append(
            {
                "outage_id": f"OUT-{i + 1:03d}",
                "region": o["region"],
                "service_type": o["service_type"],
                "start_date": params.start_ts + pd.Timedelta(hours=start_h),
                "end_date": params.start_ts + pd.Timedelta(hours=start_h + o["hours"]),
                "cause": o["cause"],
                "hidden_share": o["share"],
            }
        )
    cols = [
        "outage_id",
        "region",
        "service_type",
        "start_date",
        "end_date",
        "cause",
        "hidden_share",
    ]
    return pd.DataFrame(rows, columns=cols)


# ======================================================================================
# Arrival process
# ======================================================================================
def _hourly_intensity(params: GenerationParams) -> tuple[pd.DatetimeIndex, np.ndarray]:
    hours = pd.date_range(params.start_ts, params.end_ts, freq="h", inclusive="left")
    local = hours.tz_convert(C.LOCAL_TZ_FOR_PROFILE)
    wd = np.array(C.WEEKDAY_PROFILE)[local.dayofweek]
    hr = np.array(C.HOURLY_PROFILE)[local.hour]
    month = np.array([C.MONTH_PROFILE.get(m, 1.0) for m in local.month])
    years = (hours - params.start_ts).total_seconds().to_numpy() / (365.25 * 86400)
    growth = (1 + C.ANNUAL_GROWTH) ** years
    return hours, wd * hr * month * growth


def sample_baseline_times(rng: np.random.Generator, params: GenerationParams, n: int) -> np.ndarray:
    """Seconds since window start for n baseline tickets (seasonal Poisson-like process)."""
    hours, intensity = _hourly_intensity(params)
    counts = rng.multinomial(n, intensity / intensity.sum())
    hour_offsets = (hours - params.start_ts).total_seconds().to_numpy()
    starts = np.repeat(hour_offsets, counts)
    return np.sort(starts + rng.uniform(0, 3600, size=n))


# ======================================================================================
# Ticket simulation
# ======================================================================================
def _local_features(
    ts_seconds: np.ndarray, start: pd.Timestamp, tz_per_ticket: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """After-hours and weekend flags evaluated in each site's local timezone."""
    utc = pd.DatetimeIndex(start + pd.to_timedelta(ts_seconds, unit="s"))
    after_hours = np.zeros(len(ts_seconds), dtype=bool)
    weekend = np.zeros(len(ts_seconds), dtype=bool)
    for tz in pd.unique(tz_per_ticket):
        mask = tz_per_ticket == tz
        local = utc[mask].tz_convert(tz)
        h = local.hour.to_numpy()
        lo, hi = C.BUSINESS_HOURS
        after_hours[mask] = (h < lo) | (h >= hi)
        weekend[mask] = local.dayofweek.to_numpy() >= 5
    return after_hours, weekend


def _simulate_backlog(
    t: np.ndarray,
    groups: np.ndarray,
    base_log_hours: np.ndarray,
    capacity: dict[str, float] | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Sequential pass in creation order: the group's open backlog feeds resolution time.

    With ``capacity=None`` the feedback is disabled (used to calibrate capacities).
    Returns (open_backlog_at_creation, active_hours) where active_hours is how long the
    ticket stays open until first resolution or cancellation.
    """
    n = len(t)
    backlog = np.zeros(n, dtype=np.int64)
    hours = np.zeros(n, dtype=float)
    heaps: dict[str, list[float]] = {}
    for i in range(n):
        g = groups[i]
        heap = heaps.setdefault(g, [])
        now = t[i]
        while heap and heap[0] <= now:
            heapq.heappop(heap)
        b = len(heap)
        backlog[i] = b
        extra = 0.0
        if capacity is not None:
            ratio = b / capacity[g]
            extra = C.BACKLOG_EFFECT * min(max(0.0, ratio - 1.0), C.BACKLOG_EFFECT_MAX_RATIO)
        h = max(float(np.exp(base_log_hours[i] + extra)), 0.05)
        hours[i] = h
        heapq.heappush(heap, now + h * 3600.0)
    return backlog, hours


def _calibrate_capacity(backlog: np.ndarray, groups: np.ndarray) -> dict[str, float]:
    """Capacity per group from the no-feedback backlog distribution (size-invariant)."""
    out = {}
    for g in np.unique(groups):
        q = float(np.quantile(backlog[groups == g], C.BACKLOG_CAPACITY_QUANTILE))
        out[str(g)] = max(C.BACKLOG_CAPACITY_MULTIPLIER * q, 3.0)
    return out


def simulate_tickets(
    rng: np.random.Generator,
    params: GenerationParams,
    customers: pd.DataFrame,
    sites: pd.DataFrame,
    services: pd.DataFrame,
    outages: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    n_total = params.n_tickets
    window_s = (params.end_ts - params.start_ts).total_seconds()

    svc = services.merge(sites[["site_id", "region", "timezone"]], on="site_id").merge(
        customers[["customer_id", "tier", "customer_name", "hidden_ops_latent"]], on="customer_id"
    )
    svc_w = svc["hidden_volume_weight"].to_numpy()
    svc_p = svc_w / svc_w.sum()

    # ---- outage tickets -------------------------------------------------------------
    outage_counts = [int(round(s * n_total)) for s in outages["hidden_share"]]
    n_outage = sum(outage_counts)
    n_base = n_total - n_outage

    t_parts, svc_parts, src_parts = [], [], []
    t_parts.append(sample_baseline_times(rng, params, n_base))
    svc_parts.append(rng.choice(len(svc), size=n_base, p=svc_p))
    src_parts.append(np.full(n_base, -1, dtype=np.int64))

    for k, (o, cnt) in enumerate(zip(outages.itertuples(index=False), outage_counts, strict=True)):
        if cnt == 0:
            continue
        match = (svc["region"] == o.region) & (svc["service_type"] == o.service_type)
        if not match.any():
            match = svc["region"] == o.region
        idx = np.flatnonzero(match.to_numpy())
        p = svc_w[idx] / svc_w[idx].sum()
        s0 = (o.start_date - params.start_ts).total_seconds()
        dur = (o.end_date - o.start_date).total_seconds()
        t_parts.append(s0 + dur * rng.beta(1.3, 3.0, size=cnt))
        svc_parts.append(rng.choice(idx, size=cnt, p=p))
        src_parts.append(np.full(cnt, k, dtype=np.int64))

    t = np.concatenate(t_parts)
    svc_idx = np.concatenate(svc_parts)
    outage_src = np.concatenate(src_parts)
    order = np.argsort(t, kind="stable")
    t, svc_idx, outage_src = t[order], svc_idx[order], outage_src[order]
    n = len(t)
    is_outage_ticket = outage_src >= 0

    service_type = svc["service_type"].to_numpy()[svc_idx]
    region = svc["region"].to_numpy()[svc_idx]
    tz = svc["timezone"].to_numpy()[svc_idx]
    tier = svc["tier"].to_numpy()[svc_idx]
    group = svc["assignment_group"].to_numpy()[svc_idx]
    customer_id = svc["customer_id"].to_numpy()[svc_idx]
    customer_name = svc["customer_name"].to_numpy()[svc_idx]
    cust_latent = svc["hidden_ops_latent"].to_numpy()[svc_idx]
    site_id = svc["site_id"].to_numpy()[svc_idx]
    service_id = svc["service_id"].to_numpy()[svc_idx]
    city = sites.set_index("site_id").loc[site_id, "city"].to_numpy()

    # ---- categorical attributes --------------------------------------------------------
    ticket_type = _weighted_choice(rng, C.TICKET_TYPES, C.TICKET_TYPE_WEIGHTS, n)
    ticket_type[is_outage_ticket] = "incident"
    is_incident = ticket_type == "incident"

    sev_inc = _weighted_choice(rng, C.SEVERITIES, C.SEVERITY_WEIGHTS, n)
    sev_other = _weighted_choice(
        rng, C.SEVERITIES, {"critical": 0.01, "major": 0.09, "minor": 0.45, "low": 0.45}, n
    )
    sev_out = _weighted_choice(
        rng, C.SEVERITIES, {"critical": 0.35, "major": 0.45, "minor": 0.15, "low": 0.05}, n
    )
    severity = np.where(is_incident, sev_inc, sev_other)
    severity = np.where(is_outage_ticket, sev_out, severity).astype(object)

    ch_w = dict(C.CHANNEL_WEIGHTS)
    ch_all = _weighted_choice(rng, C.CHANNELS, ch_w, n)
    ch_nomon = _weighted_choice(rng, C.CHANNELS, {**ch_w, "monitoring": 0.0}, n)
    ch_out = _weighted_choice(
        rng,
        C.CHANNELS,
        {
            "phone": 0.40,
            "email": 0.08,
            "web_portal": 0.10,
            "chat": 0.05,
            "api": 0.02,
            "monitoring": 0.35,
        },
        n,
    )
    channel = np.where(is_incident, ch_all, ch_nomon)
    channel = np.where(is_outage_ticket, ch_out, channel).astype(object)

    sev_rank = {"critical": 1, "major": 2, "minor": 3, "low": 4}
    priority = np.array([sev_rank[s] for s in severity], dtype=np.int64)
    bump_up = (tier == "platinum") & (rng.random(n) < 0.5)
    bump_down = (tier == "bronze") & (rng.random(n) < 0.3)
    priority = np.clip(priority - bump_up.astype(int) + bump_down.astype(int), 1, 4)

    # ---- outage active at creation (any ticket, not only outage-driven ones) ----------
    active_outage = np.full(n, None, dtype=object)
    for o in outages.itertuples(index=False):
        s0 = (o.start_date - params.start_ts).total_seconds()
        s1 = (o.end_date - params.start_ts).total_seconds()
        m = (t >= s0) & (t < s1) & (region == o.region) & (service_type == o.service_type)
        active_outage[m] = o.outage_id
    outage_active = np.array([x is not None for x in active_outage])

    after_hours, weekend = _local_features(t, params.start_ts, tz)

    # ---- resolution-time model ----------------------------------------------------------
    target_hours = np.array(
        [C.SLA_TARGET_HOURS[ti][se] for ti, se in zip(tier, severity, strict=True)], dtype=float
    )
    group_names = sorted(pd.unique(svc["assignment_group"]))
    group_latent_map = dict(
        zip(group_names, rng.normal(0.0, C.GROUP_LATENT_SD, size=len(group_names)), strict=True)
    )
    pending_p = np.where(ticket_type == "service_request", 0.30, C.PENDING_PROBABILITY)
    is_pending = rng.random(n) < pending_p

    base_log = (
        np.log(target_hours)
        + C.BASE_LOG_OFFSET
        + np.array([C.SEVERITY_EFFECT[s] for s in severity])
        + np.array([C.TIER_EFFECT[x] for x in tier])
        + np.array([C.SERVICE_EFFECT[x] for x in service_type])
        + np.array([C.CHANNEL_EFFECT[x] for x in channel])
        + np.array([C.TICKET_TYPE_EFFECT[x] for x in ticket_type])
        + C.AFTER_HOURS_EFFECT * after_hours
        + C.WEEKEND_EFFECT * weekend
        + C.OUTAGE_EFFECT * outage_active
        + C.PENDING_EFFECT * is_pending
        + cust_latent
        + np.array([group_latent_map[g] for g in group])
        + rng.normal(0.0, C.RESOLUTION_NOISE_SD, size=n)
    )

    # two passes: measure the natural backlog, derive capacities, then re-run with feedback
    backlog0, _ = _simulate_backlog(t, group, base_log, capacity=None)
    capacity = _calibrate_capacity(backlog0, group)
    backlog, active_hours = _simulate_backlog(t, group, base_log, capacity)

    # ---- lifecycle: cancel / reopen / timeline -------------------------------------------
    cancelled = rng.random(n) < C.CANCEL_PROBABILITY
    reopened = (~cancelled) & is_incident & (rng.random(n) < C.REOPEN_PROBABILITY)
    reopen_gap_h = rng.uniform(2.0, 72.0, size=n)
    reopen_work_h = active_hours * rng.lognormal(-0.7, 0.5, size=n)

    first_end = t + active_hours * 3600.0  # first resolution or cancellation
    final_res = np.where(reopened, first_end + (reopen_gap_h + reopen_work_h) * 3600.0, first_end)

    # status timeline (seconds since window start)
    ack_delay = np.where(
        channel == "monitoring",
        rng.lognormal(np.log(120), 0.4, size=n),
        rng.lognormal(np.log(1200), 0.9, size=n) * np.where(after_hours | weekend, 2.5, 1.0),
    )
    t_inprog = np.minimum(t + ack_delay, t + 0.3 * active_hours * 3600.0)
    t_pend_start = t + rng.uniform(0.2, 0.5, size=n) * active_hours * 3600.0
    t_pend_end = t_pend_start + rng.uniform(0.2, 0.4, size=n) * active_hours * 3600.0
    t_reopen = first_end + reopen_gap_h * 3600.0
    t_closed = final_res + rng.uniform(24.0, 72.0, size=n) * 3600.0

    ids = np.array([f"TT-{i + 1:07d}" for i in range(n)], dtype=object)

    hist_parts = []

    def _add(mask: np.ndarray, status: str, when: np.ndarray, reason: str) -> None:
        hist_parts.append(
            pd.DataFrame(
                {
                    "ticket_id": ids[mask],
                    "status": status,
                    "_t": when[mask],
                    "change_reason": reason,
                }
            )
        )

    every = np.ones(n, dtype=bool)
    _add(every, "acknowledged", t, "ticket_created")
    _add(~cancelled, "in_progress", t_inprog, "assigned_to_group")
    _add(~cancelled & is_pending, "pending", t_pend_start, "waiting_on_customer")
    _add(~cancelled & is_pending, "in_progress", t_pend_end, "customer_responded")
    _add(~cancelled, "resolved", first_end, "fix_confirmed")
    _add(reopened, "in_progress", t_reopen, "customer_reopened")
    _add(reopened, "resolved", final_res, "fix_confirmed")
    _add(~cancelled, "closed", t_closed, "auto_close_after_resolution")
    _add(cancelled, "cancelled", first_end, "cancelled_by_customer")

    hist = pd.concat(hist_parts, ignore_index=True)
    hist = hist[hist["_t"] < window_s]  # snapshot: only changes that have happened
    hist = hist.sort_values(["ticket_id", "_t"], kind="stable").reset_index(drop=True)
    hist["change_date"] = params.start_ts + pd.to_timedelta(hist["_t"], unit="s")
    hist["change_date"] = hist["change_date"].dt.floor("s")

    last = hist.groupby("ticket_id", sort=False).tail(1).set_index("ticket_id")
    status = last.loc[ids, "status"].to_numpy()
    last_update = last.loc[ids, "_t"].to_numpy()

    resolved_by_snapshot = (~cancelled) & (final_res < window_s)
    resolution_s = np.where(resolved_by_snapshot, final_res, np.nan)
    reopen_count = (reopened & (t_reopen < window_s)).astype(np.int64)

    requested = rng.random(n) < 0.30
    req_res = np.where(requested, t + target_hours * rng.uniform(0.5, 1.5, size=n) * 3600.0, np.nan)

    def _ts(seconds: np.ndarray) -> pd.Series:
        s = pd.Series(params.start_ts + pd.to_timedelta(seconds, unit="s"))
        return s.dt.floor("s")

    symptoms = np.array(
        [C.SYMPTOMS[st][rng.integers(len(C.SYMPTOMS[st]))] for st in service_type], dtype=object
    )
    type_label = {
        "incident": "reports",
        "service_request": "requests",
        "query": "asks about",
        "complaint": "complains about",
    }
    name = np.array([s[0].upper() + s[1:] for s in symptoms], dtype=object)
    description = np.array(
        [
            f"{cn} {type_label[tt]} {sy} on {st.replace('_', ' ')} at {ci}. Opened via {ch}."
            for cn, tt, sy, st, ci, ch in zip(
                customer_name, ticket_type, symptoms, service_type, city, channel, strict=True
            )
        ],
        dtype=object,
    )

    tickets = pd.DataFrame(
        {
            "id": ids,
            "name": name,
            "description": description,
            "ticket_type": ticket_type,
            "severity": severity,
            "priority": priority,
            "status": status,
            "channel": channel,
            "creation_date": _ts(t),
            "last_update": _ts(last_update),
            "expected_resolution_date": _ts(t + target_hours * 3600.0),
            "requested_resolution_date": _ts(req_res),
            "resolution_date": _ts(resolution_s),
            "customer_id": customer_id,
            "site_id": site_id,
            "service_id": service_id,
            "assignment_group": group,
            "active_outage_id": active_outage,
            "open_backlog_at_creation": backlog,
            "reopen_count": reopen_count,
        },
        columns=TICKET_COLUMNS,
    )
    history = hist[HISTORY_COLUMNS].reset_index(drop=True)
    return tickets, history


# ======================================================================================
# Dirtiness (raw extract only)
# ======================================================================================
def _format_ts(series: pd.Series) -> pd.Series:
    out = series.dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return out.where(series.notna(), "")


def apply_dirtiness(rng: np.random.Generator, tickets: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Turn the clean typed frame into a messy all-string extract. Returns (raw, counts)."""
    raw = tickets.copy()
    n = len(raw)
    counts: dict[str, int] = {}

    for col in TIMESTAMP_COLUMNS:
        raw[col] = _format_ts(tickets[col])

    # mixed timestamp representations on creation_date / resolution_date
    for col in ("creation_date", "resolution_date"):
        present = tickets[col].notna().to_numpy()
        u = rng.random(n)
        offset_mask = present & (u < C.DIRTY_TZ_OFFSET_FRACTION)
        naive_mask = (
            present
            & (u >= C.DIRTY_TZ_OFFSET_FRACTION)
            & (u < C.DIRTY_TZ_OFFSET_FRACTION + C.DIRTY_NAIVE_TS_FRACTION)
        )
        local = tickets.loc[offset_mask, col].dt.tz_convert(C.LOCAL_TZ_FOR_PROFILE)
        raw.loc[offset_mask, col] = local.dt.strftime("%Y-%m-%dT%H:%M:%S%z").str.replace(
            r"(\d{2})(\d{2})$", r"\1:\2", regex=True
        )
        raw.loc[naive_mask, col] = tickets.loc[naive_mask, col].dt.strftime("%Y-%m-%d %H:%M:%S")
        counts[f"{col}_with_local_offset"] = int(offset_mask.sum())
        counts[f"{col}_naive_utc"] = int(naive_mask.sum())

    # impossible rows: resolution before creation (after the format step, so that the
    # injected count is exact and every such row is rejected downstream)
    resolved_idx = np.flatnonzero(tickets["resolution_date"].notna().to_numpy())
    k = int(round(C.DIRTY_IMPOSSIBLE_FRACTION * n))
    bad = rng.choice(resolved_idx, size=min(k, len(resolved_idx)), replace=False)
    bad_res = tickets["creation_date"].iloc[bad] - pd.to_timedelta(
        rng.uniform(1, 48, size=len(bad)), unit="h"
    )
    raw.iloc[bad, raw.columns.get_loc("resolution_date")] = _format_ts(
        bad_res.dt.floor("s")
    ).to_numpy()
    counts["impossible_resolution_before_creation"] = len(bad)

    # casing / whitespace in categoricals
    u = rng.random(n)
    case_mask = u < C.DIRTY_CASING_FRACTION
    ws_mask = (u >= C.DIRTY_CASING_FRACTION) & (
        u < C.DIRTY_CASING_FRACTION + C.DIRTY_WHITESPACE_FRACTION
    )
    for col in CATEGORICAL_COLUMNS:
        s = raw[col].astype(object)
        which = rng.random(n)
        upper = case_mask & (which < 0.5)
        title = case_mask & (which >= 0.5)
        s[upper] = s[upper].str.upper()
        s[title] = s[title].str.replace("_", " ").str.title()
        pads = np.array(["  ", "\t", " "], dtype=object)[rng.integers(0, 3, size=n)]
        s[ws_mask] = pads[ws_mask] + s[ws_mask] + " "
        raw[col] = s
    counts["categorical_case_variants_rows"] = int(case_mask.sum())
    counts["categorical_whitespace_rows"] = int(ws_mask.sum())

    # missing optional fields
    miss_desc = rng.random(n) < C.DIRTY_MISSING_DESCRIPTION_FRACTION
    raw.loc[miss_desc, "description"] = ""
    miss_pri = rng.random(n) < C.DIRTY_MISSING_PRIORITY_FRACTION
    raw["priority"] = raw["priority"].astype(object)
    raw.loc[miss_pri, "priority"] = ""
    counts["missing_description"] = int(miss_desc.sum())
    counts["missing_priority"] = int(miss_pri.sum())

    # exact duplicate rows
    k = int(round(C.DIRTY_DUPLICATE_FRACTION * n))
    dup_idx = rng.choice(n, size=k, replace=False)
    raw = pd.concat([raw, raw.iloc[dup_idx]], ignore_index=True)
    raw = raw.sort_values("id", kind="stable").reset_index(drop=True)
    counts["duplicate_rows"] = int(k)

    raw["active_outage_id"] = raw["active_outage_id"].fillna("")
    raw["open_backlog_at_creation"] = raw["open_backlog_at_creation"].astype(int)
    raw["reopen_count"] = raw["reopen_count"].astype(int)
    return raw, counts


# ======================================================================================
# Orchestration
# ======================================================================================
def generate(params: GenerationParams) -> GeneratedData:
    t0 = time.perf_counter()
    rng = np.random.default_rng(params.seed)
    customers = build_customers(rng)
    sites = build_sites(rng, customers)
    services = build_services(rng, sites)
    sla_targets = build_sla_targets()
    outages = build_outages(params)
    log.info(
        "dimensions: %d customers, %d sites, %d services, %d outage incidents",
        len(customers),
        len(sites),
        len(services),
        len(outages),
    )
    tickets, history = simulate_tickets(rng, params, customers, sites, services, outages)
    log.info("simulated %d tickets, %d status changes", len(tickets), len(history))
    raw_tickets, dirt = apply_dirtiness(rng, tickets)

    resolved = tickets["resolution_date"].notna()
    res_h = (tickets["resolution_date"] - tickets["creation_date"]).dt.total_seconds() / 3600
    exp_h = (
        tickets["expected_resolution_date"] - tickets["creation_date"]
    ).dt.total_seconds() / 3600
    breach_rate = float((res_h[resolved] > exp_h[resolved]).mean())
    elapsed = time.perf_counter() - t0
    log.info("clean breach rate among resolved tickets: %.3f (%.1fs)", breach_rate, elapsed)

    metadata = {
        "synthetic": True,
        "disclaimer": "Synthetic data generated by slawatch. No real customers, sites or tickets.",
        "generator_version": __version__,
        "params": {
            "seed": params.seed,
            "n_tickets": params.n_tickets,
            "start": params.start,
            "end": params.end,
        },
        "row_counts": {
            "customers": int(len(customers)),
            "sites": int(len(sites)),
            "services": int(len(services)),
            "outage_incidents": int(len(outages)),
            "tickets_clean": int(len(tickets)),
            "tickets_raw_extract": int(len(raw_tickets)),
            "status_history": int(len(history)),
        },
        "clean_breach_rate_resolved": round(breach_rate, 4),
        "status_counts": {k: int(v) for k, v in tickets["status"].value_counts().items()},
        "dirtiness_injected": dirt,
        "generation_seconds": round(elapsed, 2),
    }
    return GeneratedData(
        customers=customers.drop(columns=["hidden_ops_latent"]),
        sites=sites.drop(columns=["hidden_volume_weight"]),
        services=services.drop(columns=["hidden_volume_weight"]),
        sla_targets=sla_targets,
        outages=outages.drop(columns=["hidden_share"]),
        tickets=tickets,
        status_history=history,
        raw_tickets=raw_tickets,
        metadata=metadata,
    )


def write_outputs(data: GeneratedData, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    data.customers.to_csv(out_dir / FILE_NAMES["customers"], index=False)
    data.sites.to_csv(out_dir / FILE_NAMES["sites"], index=False)
    data.services.to_csv(out_dir / FILE_NAMES["services"], index=False)
    data.sla_targets.to_csv(out_dir / FILE_NAMES["sla_targets"], index=False)
    outages = data.outages.copy()
    for col in ("start_date", "end_date"):
        outages[col] = _format_ts(outages[col])
    outages.to_csv(out_dir / FILE_NAMES["outages"], index=False)
    data.raw_tickets.to_csv(out_dir / FILE_NAMES["tickets"], index=False)
    hist = data.status_history.copy()
    hist["change_date"] = _format_ts(hist["change_date"])
    hist.to_csv(out_dir / FILE_NAMES["status_history"], index=False)
    (out_dir / FILE_NAMES["metadata"]).write_text(json.dumps(data.metadata, indent=2) + "\n")
    log.info("wrote %d files to %s", len(FILE_NAMES), out_dir)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate synthetic TMF621-shaped trouble tickets.")
    p.add_argument("--seed", type=int, default=C.DEFAULT_SEED)
    p.add_argument("--n-tickets", type=int, default=C.DEFAULT_N_TICKETS)
    p.add_argument("--start", default=C.DEFAULT_START, help="window start, YYYY-MM-DD (UTC)")
    p.add_argument("--end", default=C.DEFAULT_END, help="window end (exclusive) and snapshot date")
    p.add_argument("--out", type=Path, default=Path("data/raw"))
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    params = GenerationParams(
        seed=args.seed, n_tickets=args.n_tickets, start=args.start, end=args.end
    )
    data = generate(params)
    write_outputs(data, args.out)


if __name__ == "__main__":
    main()
