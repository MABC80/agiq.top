"""Process memory, for the health check and the startup log. No secrets."""


def rss_kb() -> int:
    """Resident set size in kilobytes, from the Linux process status."""
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return 0
    return 0
