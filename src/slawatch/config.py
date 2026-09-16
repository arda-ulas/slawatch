"""Domain configuration for the synthetic telecom enterprise service desk.

Everything here is invented. Customer names, sites and volumes are synthetic and do not
describe any real company. See docs/data.md for how these tables are used.
"""

from __future__ import annotations

# --------------------------------------------------------------------------------------
# Time window and size defaults
# --------------------------------------------------------------------------------------
DEFAULT_SEED = 20240701
DEFAULT_N_TICKETS = 200_000
DEFAULT_START = "2024-07-01"
DEFAULT_END = "2026-07-01"  # exclusive; also the "as of" snapshot for open tickets

# --------------------------------------------------------------------------------------
# Customers: tier, industry and SLA
# --------------------------------------------------------------------------------------
TIERS = ["platinum", "gold", "silver", "bronze"]
TIER_WEIGHTS = {"platinum": 0.15, "gold": 0.30, "silver": 0.35, "bronze": 0.20}

INDUSTRIES = [
    "financial_services",
    "healthcare",
    "retail",
    "manufacturing",
    "public_sector",
    "logistics",
    "education",
    "energy",
]

SEVERITIES = ["critical", "major", "minor", "low"]
# Probability of each severity for a baseline (non-outage) incident.
SEVERITY_WEIGHTS = {"critical": 0.07, "major": 0.23, "minor": 0.45, "low": 0.25}

# Contracted resolution targets in hours, by customer tier x ticket severity.
SLA_TARGET_HOURS: dict[str, dict[str, float]] = {
    "platinum": {"critical": 4, "major": 8, "minor": 24, "low": 72},
    "gold": {"critical": 6, "major": 12, "minor": 48, "low": 96},
    "silver": {"critical": 8, "major": 24, "minor": 72, "low": 120},
    "bronze": {"critical": 12, "major": 36, "minor": 96, "low": 168},
}

TICKET_TYPES = ["incident", "service_request", "query", "complaint"]
TICKET_TYPE_WEIGHTS = {"incident": 0.72, "service_request": 0.18, "query": 0.07, "complaint": 0.03}

CHANNELS = ["phone", "email", "web_portal", "chat", "api", "monitoring"]
CHANNEL_WEIGHTS = {
    "phone": 0.30,
    "email": 0.22,
    "web_portal": 0.20,
    "chat": 0.08,
    "api": 0.05,
    "monitoring": 0.15,
}

STATUSES = ["acknowledged", "in_progress", "pending", "held", "resolved", "closed", "cancelled"]

# --------------------------------------------------------------------------------------
# Geography: Canadian provinces grouped into operating regions
# --------------------------------------------------------------------------------------
REGIONS = {
    "ontario": ["ON"],
    "quebec": ["QC"],
    "west": ["BC", "AB"],
    "prairies": ["SK", "MB"],
    "atlantic": ["NS", "NB", "NL", "PE"],
}
PROVINCE_TO_REGION = {p: r for r, ps in REGIONS.items() for p in ps}

# (city, province, timezone, weight) - weights drive how many sites land in each city.
CITIES = [
    ("Toronto", "ON", "America/Toronto", 18),
    ("Ottawa", "ON", "America/Toronto", 6),
    ("Mississauga", "ON", "America/Toronto", 5),
    ("Hamilton", "ON", "America/Toronto", 3),
    ("London", "ON", "America/Toronto", 3),
    ("Kitchener", "ON", "America/Toronto", 3),
    ("Windsor", "ON", "America/Toronto", 2),
    ("Kingston", "ON", "America/Toronto", 2),
    ("Sudbury", "ON", "America/Toronto", 1),
    ("Montreal", "QC", "America/Toronto", 14),
    ("Quebec City", "QC", "America/Toronto", 5),
    ("Laval", "QC", "America/Toronto", 3),
    ("Gatineau", "QC", "America/Toronto", 2),
    ("Sherbrooke", "QC", "America/Toronto", 2),
    ("Vancouver", "BC", "America/Vancouver", 10),
    ("Surrey", "BC", "America/Vancouver", 3),
    ("Victoria", "BC", "America/Vancouver", 2),
    ("Kelowna", "BC", "America/Vancouver", 1),
    ("Calgary", "AB", "America/Edmonton", 8),
    ("Edmonton", "AB", "America/Edmonton", 6),
    ("Red Deer", "AB", "America/Edmonton", 1),
    ("Winnipeg", "MB", "America/Winnipeg", 4),
    ("Regina", "SK", "America/Regina", 2),
    ("Saskatoon", "SK", "America/Regina", 2),
    ("Halifax", "NS", "America/Halifax", 4),
    ("Moncton", "NB", "America/Moncton", 2),
    ("Fredericton", "NB", "America/Moncton", 1),
    ("St. John's", "NL", "America/St_Johns", 2),
    ("Charlottetown", "PE", "America/Halifax", 1),
]

# --------------------------------------------------------------------------------------
# Services and assignment groups
# --------------------------------------------------------------------------------------
SERVICE_TYPES = [
    "dedicated_internet",
    "sd_wan",
    "business_voice_sip",
    "managed_cloud",
    "managed_security",
    "mpls_wan",
]
# Share of service instances per type, and relative ticket volume per instance.
SERVICE_TYPE_WEIGHTS = {
    "dedicated_internet": 0.30,
    "sd_wan": 0.20,
    "business_voice_sip": 0.20,
    "managed_cloud": 0.10,
    "managed_security": 0.10,
    "mpls_wan": 0.10,
}
SERVICE_TICKET_RATE = {
    "dedicated_internet": 1.0,
    "sd_wan": 1.3,
    "business_voice_sip": 1.1,
    "managed_cloud": 0.9,
    "managed_security": 0.7,
    "mpls_wan": 0.8,
}
# Services routed to a regional field group vs a national specialist team.
SERVICE_ASSIGNMENT = {
    "dedicated_internet": "regional",
    "sd_wan": "regional",
    "mpls_wan": "regional",
    "business_voice_sip": "voice_ops",
    "managed_cloud": "cloud_ops",
    "managed_security": "security_ops",
}
BANDWIDTH_OPTIONS_MBPS = [50, 100, 200, 500, 1000, 2000, 10000]

# --------------------------------------------------------------------------------------
# Seasonality profiles (relative intensities; normalised at generation time)
# --------------------------------------------------------------------------------------
# Monday=0 ... Sunday=6
WEEKDAY_PROFILE = [1.18, 1.12, 1.06, 1.02, 0.95, 0.42, 0.36]
# Hour of day (local business time, approximated as America/Toronto for the whole book).
HOURLY_PROFILE = [
    0.22,
    0.18,
    0.16,
    0.16,
    0.18,
    0.28,  # 00-05
    0.45,
    0.80,
    1.40,
    1.75,
    1.70,
    1.55,  # 06-11
    1.25,
    1.50,
    1.55,
    1.45,
    1.25,
    0.95,  # 12-17
    0.65,
    0.50,
    0.42,
    0.38,
    0.32,
    0.26,  # 18-23
]
ANNUAL_GROWTH = 0.08  # ticket volume grows ~8 %/year
MONTH_PROFILE = {12: 0.85, 1: 1.05, 2: 1.02, 7: 0.92, 8: 0.93}  # holiday dip, Jan bump

BUSINESS_HOURS = (8, 18)  # local, [start, end)
LOCAL_TZ_FOR_PROFILE = "America/Toronto"

# --------------------------------------------------------------------------------------
# Resolution-time model (log-hours, relative to the SLA target)
# --------------------------------------------------------------------------------------
# log(resolution_hours) = log(target_hours) + BASE_LOG_OFFSET + sum(effects) + noise
BASE_LOG_OFFSET = -1.30
RESOLUTION_NOISE_SD = 0.62
SEVERITY_EFFECT = {"critical": -0.20, "major": -0.05, "minor": 0.05, "low": 0.15}
TIER_EFFECT = {"platinum": -0.12, "gold": -0.04, "silver": 0.04, "bronze": 0.10}
SERVICE_EFFECT = {
    "dedicated_internet": 0.00,
    "sd_wan": 0.18,
    "business_voice_sip": 0.05,
    "managed_cloud": -0.05,
    "managed_security": -0.15,
    "mpls_wan": 0.10,
}
CHANNEL_EFFECT = {
    "phone": 0.00,
    "email": 0.22,
    "web_portal": 0.08,
    "chat": 0.05,
    "api": -0.05,
    "monitoring": -0.25,
}
TICKET_TYPE_EFFECT = {"incident": 0.0, "service_request": 0.10, "query": -0.30, "complaint": 0.15}
AFTER_HOURS_EFFECT = 0.28
WEEKEND_EFFECT = 0.35
OUTAGE_EFFECT = 0.45
# Backlog pressure: group capacity is calibrated per run as
#   BACKLOG_CAPACITY_MULTIPLIER x (BACKLOG_CAPACITY_QUANTILE of the no-feedback backlog),
# and the effect is BACKLOG_EFFECT x clip(backlog/capacity - 1, 0, BACKLOG_EFFECT_MAX_RATIO),
# i.e. it saturates so the queue cannot run away.
BACKLOG_CAPACITY_QUANTILE = 0.75
BACKLOG_CAPACITY_MULTIPLIER = 1.3
BACKLOG_EFFECT = 0.40
BACKLOG_EFFECT_MAX_RATIO = 1.5
PENDING_EFFECT = 0.55  # tickets that wait on the customer
CUSTOMER_LATENT_SD = 0.18
GROUP_LATENT_SD = 0.10
PENDING_PROBABILITY = 0.14
REOPEN_PROBABILITY = 0.04
CANCEL_PROBABILITY = 0.015

# --------------------------------------------------------------------------------------
# Outage incidents (regional spikes). Offsets are days from --start; durations in hours.
# Each spike gets `share` of the total ticket count on top of the baseline process.
# --------------------------------------------------------------------------------------
OUTAGE_INCIDENTS = [
    dict(
        day=41,
        hours=9,
        region="ontario",
        service_type="dedicated_internet",
        share=0.006,
        cause="core_router_failure",
    ),
    dict(
        day=118,
        hours=30,
        region="quebec",
        service_type="business_voice_sip",
        share=0.008,
        cause="sbc_software_defect",
    ),
    dict(
        day=205,
        hours=14,
        region="west",
        service_type="sd_wan",
        share=0.005,
        cause="controller_certificate_expiry",
    ),
    dict(
        day=233,
        hours=48,
        region="atlantic",
        service_type="dedicated_internet",
        share=0.004,
        cause="ice_storm_fibre_cuts",
    ),
    dict(
        day=356,
        hours=6,
        region="ontario",
        service_type="managed_cloud",
        share=0.004,
        cause="storage_array_degradation",
    ),
    dict(
        day=452,
        hours=20,
        region="prairies",
        service_type="mpls_wan",
        share=0.003,
        cause="fibre_cut_construction",
    ),
    dict(
        day=529,
        hours=11,
        region="ontario",
        service_type="business_voice_sip",
        share=0.007,
        cause="ddos_on_sip_edge",
    ),
    dict(
        day=641,
        hours=36,
        region="quebec",
        service_type="sd_wan",
        share=0.006,
        cause="firmware_rollout_regression",
    ),
]

# --------------------------------------------------------------------------------------
# Dirtiness injected into the raw ticket extract (fractions of rows)
# --------------------------------------------------------------------------------------
DIRTY_CASING_FRACTION = 0.03
DIRTY_WHITESPACE_FRACTION = 0.02
DIRTY_DUPLICATE_FRACTION = 0.003
DIRTY_MISSING_DESCRIPTION_FRACTION = 0.05
DIRTY_MISSING_PRIORITY_FRACTION = 0.01
DIRTY_TZ_OFFSET_FRACTION = 0.05
DIRTY_NAIVE_TS_FRACTION = 0.02
DIRTY_IMPOSSIBLE_FRACTION = 0.0008

# --------------------------------------------------------------------------------------
# Synthetic naming material
# --------------------------------------------------------------------------------------
CUSTOMER_NAME_PARTS_A = [
    "Northshore",
    "Maple",
    "Boreal",
    "Granite",
    "Harbour",
    "Prairie",
    "Summit",
    "Cedar",
    "Lakeview",
    "Ironwood",
    "Silverline",
    "Bluewater",
    "Tundra",
    "Aurora",
    "Meridian",
    "Cascadia",
    "Redwood",
    "Glacier",
    "Beacon",
    "Keystone",
    "Crestview",
    "Pinecrest",
    "Evergreen",
    "Highland",
    "Riverbend",
    "Stonebridge",
    "Trillium",
    "Westgate",
    "Ashford",
    "Coastal",
    "Dominion",
    "Frontier",
    "Heritage",
    "Juniper",
    "Lighthouse",
    "Nordic",
    "Orchard",
    "Pioneer",
    "Quarry",
    "Ridgeline",
]
INDUSTRY_NAME_SUFFIX = {
    "financial_services": ["Financial Group", "Credit Union", "Capital Partners", "Insurance"],
    "healthcare": ["Health Network", "Medical Centres", "Pharmacies", "Care Group"],
    "retail": ["Retail Group", "Stores Ltd.", "Marketplaces", "Outfitters"],
    "manufacturing": ["Manufacturing", "Industries", "Fabrication", "Automotive Parts"],
    "public_sector": ["Regional Authority", "School Board", "Transit Commission", "Utilities"],
    "logistics": ["Logistics", "Freight Lines", "Distribution", "Courier"],
    "education": ["College", "Polytechnic", "Learning Trust", "Academy"],
    "energy": ["Energy", "Power Co-op", "Pipelines", "Renewables"],
}

SYMPTOMS = {
    "dedicated_internet": [
        "circuit down",
        "intermittent packet loss",
        "high latency to cloud provider",
        "link flapping",
        "throughput below contracted rate",
        "DNS resolution failures",
        "BGP session reset",
        "CPE unreachable",
    ],
    "sd_wan": [
        "tunnel down to hub",
        "edge appliance offline",
        "policy push failed",
        "application steering not working",
        "VPN failover not triggering",
        "high jitter on voice class",
        "orchestrator sync error",
        "WAN link brownout",
    ],
    "business_voice_sip": [
        "one-way audio",
        "inbound calls failing",
        "SIP registration dropping",
        "call quality degraded",
        "DID not routing",
        "trunk capacity exceeded",
        "voicemail not delivering",
        "E911 address update",
    ],
    "managed_cloud": [
        "VM unreachable",
        "backup job failed",
        "storage latency spike",
        "hypervisor host alarm",
        "snapshot restore request",
        "capacity expansion",
        "monitoring agent offline",
        "patch window overrun",
    ],
    "managed_security": [
        "firewall rule change",
        "IPS false positive blocking traffic",
        "VPN client cannot connect",
        "SIEM alert triage",
        "certificate renewal",
        "phishing campaign report",
        "firewall HA failover",
        "log forwarding stopped",
    ],
    "mpls_wan": [
        "site isolated from WAN",
        "QoS marking dropped",
        "CE router crash",
        "PE link errors",
        "bandwidth upgrade",
        "route leak between VRFs",
        "latency between sites",
        "LDP session down",
    ],
}
