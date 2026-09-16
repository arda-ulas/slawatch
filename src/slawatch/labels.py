"""Human-readable labels for the synthetic domain's snake_case codes (report + Tableau)."""

from __future__ import annotations

SERVICE_LABELS = {
    "dedicated_internet": "Dedicated Internet",
    "sd_wan": "SD-WAN",
    "business_voice_sip": "Business Voice (SIP)",
    "managed_cloud": "Managed Cloud",
    "managed_security": "Managed Security",
    "mpls_wan": "MPLS WAN",
}

CHANNEL_LABELS = {
    "phone": "Phone",
    "email": "Email",
    "web_portal": "Web Portal",
    "chat": "Chat",
    "api": "API",
    "monitoring": "Monitoring",
}

GROUP_LABELS = {
    "cloud_ops": "Cloud Ops",
    "security_ops": "Security Ops",
    "voice_ops": "Voice Ops",
    "field_ontario": "Field - Ontario",
    "field_quebec": "Field - Quebec",
    "field_west": "Field - West",
    "field_prairies": "Field - Prairies",
    "field_atlantic": "Field - Atlantic",
}

SPLIT_LABELS = {
    "train": "Train",
    "validation": "Validation",
    "test": "Test",
    "unlabelled": "Unlabelled (open or cancelled)",
}


ACRONYMS = {
    "Sbc": "SBC",
    "Ddos": "DDoS",
    "Sip": "SIP",
    "Api": "API",
    "Hq": "HQ",
    "Mpls": "MPLS",
    "Cpe": "CPE",
    "Vpn": "VPN",
    "Dns": "DNS",
    "Bgp": "BGP",
    "Wan": "WAN",
    "Sd": "SD",
}


def humanise(code: object) -> object:
    """'financial_services' -> 'Financial Services'; leaves non-strings and NULLs alone."""
    if not isinstance(code, str):
        return code
    words = code.replace("_", " ").title().split(" ")
    return " ".join(ACRONYMS.get(w, w) for w in words)


def service_label(code: object) -> object:
    return SERVICE_LABELS.get(code, humanise(code)) if isinstance(code, str) else code


def channel_label(code: object) -> object:
    return CHANNEL_LABELS.get(code, humanise(code)) if isinstance(code, str) else code


def group_label(code: object) -> object:
    return GROUP_LABELS.get(code, humanise(code)) if isinstance(code, str) else code
