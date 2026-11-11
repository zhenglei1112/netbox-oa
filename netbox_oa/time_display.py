"""Shared display for elapsed durations, in whole minutes."""


def format_duration(minutes):
    minutes = max(0, int(minutes))
    days, remainder = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"{days}天{hours}小时{minutes}分钟"
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"
