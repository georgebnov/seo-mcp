"""The simple parameters of get_seo_data: a time frame, 1 page, 1 query, and a comparison."""

import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

# The Search Console API refuses a longer filter expression.
MAX_TEXT = 4096
MAX_LAST_DAYS = 3650
DATE_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}")
RELATIVE = re.compile(r"(\d+)daysAgo")
# The server works in days and reads no zone. Each place on Earth is on the UTC date, the day
# before, or the day after (UTC-12 to UTC+14), so a check of a day allows this many days.
SLACK_DAYS = 1
DATES = frozenset({"start_date", "end_date"})
# A comparison needs the same power as the parameter that it repeats.
COMPARE_NEEDS = {
    "compare_start_date": "start_date",
    "compare_end_date": "end_date",
    "compare_page": "page",
}


class BadInput(Exception):
    """The input is wrong for every source. The text names a parameter, never its value."""


@dataclass(frozen=True)
class Params:
    start: date | None = None
    end: date | None = None
    last_days: int | None = None
    page: str | None = None
    query: str | None = None

    def applied(self) -> dict[str, Any]:
        values = {
            "start_date": self.start.isoformat() if self.start else None,
            "end_date": self.end.isoformat() if self.end else None,
            "last_days": self.last_days,
            "page": self.page,
            "query": self.query,
        }
        return {name: value for name, value in values.items() if value is not None}


NONE = Params()


def today(at: datetime | None = None) -> date:
    """The 1 day rule of the server: the UTC date of now, or of at. It reads no zone."""
    return (at or datetime.now(UTC)).astimezone(UTC).date()


def iso_day(text: object) -> date | None:
    if not isinstance(text, str) or not DATE_SHAPE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def days_ago(text: object) -> int | None:
    """The N of a relative GA4 date. GA4 reads it in the property zone, so no day is known here."""
    if text in ("today", "yesterday"):
        return 0 if text == "today" else 1
    found = RELATIVE.fullmatch(text) if isinstance(text, str) else None
    return int(found[1]) if found else None


def bounds(text: object, day: date) -> tuple[date, date] | None:
    """The first and the last day that text can name in some place, on the UTC date day."""
    ago = days_ago(text)
    if ago is not None:
        return day - timedelta(days=ago + SLACK_DAYS), day - timedelta(days=ago - SLACK_DAYS)
    found = iso_day(text)
    return (found, found) if found else None


def length(start: object, end: object, day: date) -> tuple[int, int] | None:
    """The fewest and the most days of a range. 2 relative dates resolve in 1 place: exact."""
    first, last = days_ago(start), days_ago(end)
    if first is not None and last is not None:
        return first - last + 1, first - last + 1
    begin, finish = bounds(start, day), bounds(end, day)
    if not begin or not finish:
        return None
    return (finish[0] - begin[1]).days + 1, (finish[1] - begin[0]).days + 1


def day(name: str, text: str) -> date:
    found = iso_day(text)
    if not found:
        raise BadInput(f"{name} must be a real date in the form YYYY-MM-DD.")
    return found


def period(
    first: str, last: str, start_text: str | None, end_text: str | None
) -> tuple[date | None, date | None]:
    if start_text is None and end_text is None:
        return None, None
    if start_text is None or end_text is None:
        raise BadInput(f"{first} and {last} go together. Give both or neither.")
    start, end = day(first, start_text), day(last, end_text)
    if start > end:
        raise BadInput(f"{first} must not be after {last}.")
    return start, end


def moved(asked: object, sent: object) -> dict[str, str]:
    """Each source has its own newest day, so a source moves an end date and shows the move."""
    return {"asked_end_date": str(asked), "sent_end_date": str(sent)}


def moved_note(item: dict[str, str]) -> str:
    return (
        f"The end date moved from {item['asked_end_date']} to {item['sent_end_date']}: "
        "this source has no later day."
    )


def too_late(name: str, latest: object) -> BadInput:
    return BadInput(
        f"{name} is after the most recent day of this source ({latest}), so no range is left. "
        "The server sent nothing."
    )


def page_url(host: str, value: str, name: str = "page") -> str:
    """Give the full URL of a page on this site. A URL of another host is refused, not rewritten."""
    wrong = f"{name} must be a path that starts with / or an https URL on {host}."
    if not isinstance(value, str):
        raise BadInput(wrong)
    if not value or len(value) > MAX_TEXT or re.search(r"[\s\x00-\x1f#]", value):
        raise BadInput(f"{wrong} It holds no space or fragment, and at most {MAX_TEXT} characters.")
    if value.startswith("/") and not value.startswith("//"):
        return f"https://{host}{value}"
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.netloc.lower() != host:
        raise BadInput(wrong)
    rest = value.split(parts.netloc, 1)[1]
    return f"https://{host}{rest or '/'}"


def _query(text: str | None) -> str | None:
    if text is not None and not 1 <= len(text) <= MAX_TEXT:
        raise BadInput(f"query must have 1 to {MAX_TEXT} characters.")
    return text


def parse(
    host: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    last_days: int | None = None,
    page: str | None = None,
    query: str | None = None,
    compare_start_date: str | None = None,
    compare_end_date: str | None = None,
    compare_page: str | None = None,
) -> tuple[Params, Params | None, set[str]]:
    """Give the main params, the params of the comparison, and the names that the call used."""
    start, end = period("start_date", "end_date", start_date, end_date)
    if last_days is not None:
        if start:
            raise BadInput("Give last_days or start_date and end_date, not both.")
        if not 1 <= last_days <= MAX_LAST_DAYS:
            raise BadInput(f"last_days must be 1 to {MAX_LAST_DAYS}.")
    primary = Params(
        start,
        end,
        last_days,
        page_url(host, page) if page is not None else None,
        _query(query),
    )
    other_start, other_end = period(
        "compare_start_date", "compare_end_date", compare_start_date, compare_end_date
    )
    if other_start and compare_page is not None:
        raise BadInput("Give compare_page or compare dates, not both: compare 1 thing.")
    comparison = None
    if other_start:
        if not start:
            raise BadInput("compare_start_date needs start_date and end_date.")
        comparison = replace(primary, start=other_start, end=other_end)
    if compare_page is not None:
        if primary.page is None:
            raise BadInput("compare_page needs page.")
        other_page = page_url(host, compare_page, "compare_page")
        if other_page == primary.page:
            raise BadInput("compare_page must differ from page.")
        comparison = replace(primary, page=other_page)
    named = {
        "start_date": start_date,
        "end_date": end_date,
        "last_days": last_days,
        "page": page,
        "query": query,
        "compare_start_date": compare_start_date,
        "compare_end_date": compare_end_date,
        "compare_page": compare_page,
    }
    return primary, comparison, {name for name, value in named.items() if value is not None}
