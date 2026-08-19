import os

_ANNOUNCEMENTS_ENABLED_ENV = "TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED"


def announcements_enabled() -> bool:
    """Return whether CLI startup announcements are enabled for this process."""
    value = os.environ.get(_ANNOUNCEMENTS_ENABLED_ENV)
    if value is None:
        return True

    normalized = value.strip().lower()
    if not normalized or normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(
        "Invalid value for TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED: expected one of "
        "true/1/yes/on/false/0/no/off"
    )


CLI_CONFIG = {
    # Announcements
    "announcements_url": "https://api.tauric.ai/v1/announcements",
    "announcements_timeout": 1.0,
    "announcements_fallback": "[cyan]For more information, please visit[/cyan] [link=https://github.com/TauricResearch]https://github.com/TauricResearch[/link]",
}
