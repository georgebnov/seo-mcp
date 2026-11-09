"""Find the call for a thing that an agent wants. This module makes no network call.

Each task lists, for each provider that can answer it, the steps to call and how to read
the answer. Marks in braces become real dates here. Marks in angle brackets are input that
only the agent has. tests/test_tasks.py runs each step through the real tools.
"""

import re
from datetime import date, timedelta
from typing import Any, NamedTuple

from seo_mcp import params

CLARITY_DAYS = 90
# Clarity keeps playback data for 30 days, so a sample has no older session. 28 days stays
# inside that with a margin, has 4 whole weeks, and shows fewer elements that a site removed.
CLICK_DAYS = 28
RECORDING_DAYS = 30
RETENTION_DOCS = "https://learn.microsoft.com/en-us/clarity/setup-and-installation/data-retention"
SEARCH_DAYS = 28
# Search Console data ends about 3 days before the call. A YYYY-MM-DD end of a task with a lag
# takes 1 more day, so that it is complete in each zone: the server reads no zone.
SEARCH_LAG_DAYS = 3
# A trimmed session row is about 0.6 KB. 20 rows fit the tool output of an agent with room for
# the work around them, and the newest sessions show the elements of the site of today.
SAMPLE_SESSIONS = 20
INPUT_MARK = re.compile(r"<[a-z_]+>")

CLARITY_QUOTA = (
    "Each step spends 1 of the 10 Clarity calls of the project for the UTC day. Read calls_left "
    "in quota, or in list_sites, before you start."
)
ZERO_COUNT_STOP = (
    "If the dashboard count is 0 (no page has a count above 0), do not call recordings: it "
    "spends 1 call on a sample that cannot show such a click. Report the 0 and stop. The "
    "server enforces this: after a saved 0 count of the same click and days, recordings gives "
    "status skipped and spends nothing."
)
OLD_DATA = (
    "Empty rows do not prove zero activity. Clarity refuses no time frame, and it keeps "
    f"dashboard data for 9 months ({RETENTION_DOCS}): rows of old days are simply absent. "
    "Try a longer window."
)
RECORDING_AGE = (
    f"Clarity keeps recordings for {RECORDING_DAYS} days ({RETENTION_DOCS}), so the window "
    f"is at most {RECORDING_DAYS} days. Empty rows do not prove zero activity. Set start_date "
    "only when the layout or the elements of the studied page changed, such as a moved button "
    "or a new form. New content on other pages, such as a new blog post, does not reset the "
    "window. After such a change, ask the task again with start_date set to that day: then "
    "both steps show only the new design of the page."
)
OTHER_HOSTS = (
    "Rows can hold other hosts, such as localhost, when the tag ran there. Keep only the "
    "pages of the site."
)
COUNT_ONLY = (
    "Column names come from the question and can change. Read answered_question for the "
    "question and the dates that Clarity used."
)
SESSION_LINKS = "Each session row has a link to a recording: keep links out of public text."


class Step(NamedTuple):
    why: str
    tool: str
    args: dict[str, Any]
    read: str
    skip_if: str = ""


class Count(NamedTuple):
    """A dashboard count that the server reads before a sample of the same click."""

    question: str
    words: str


# The recordings flag of each click task. clarity._recordings reads the saved count of its
# question, so that a sample after a 0 count does not go out.
COUNTS = {
    "deadClickPresent": Count("Dead click count per page", "dead click"),
    "rageClickPresent": Count("Rage click count per page", "rage click"),
    "quickbackClickPresent": Count("Quickback click count per page", "quickback click"),
}


def count_query(question: str, start: str, end: str) -> str:
    return f"{question} between {start} and {end}"


class Recipe(NamedTuple):
    api: str
    steps: tuple[Step, ...] = ()
    means: str = ""
    limits: tuple[str, ...] = ()
    not_available: str = ""


class Task(NamedTuple):
    summary: str
    asks: tuple[str, ...]
    recipes: tuple[Recipe, ...]
    days: int = CLARITY_DAYS
    lag: int = 0
    compare: bool = False
    # The most days that a start_date can give. 0 means no limit.
    max_days: int = 0


def _question(text: str, why: str, read: str) -> Step:
    request = {"query": count_query(text, "{start_slash}", "{end_slash}")}
    args = {"api": "clarity", "method": "dashboard_query", "request": request}
    return Step(why, "query_api", args, read)


def _sessions(flag: str, read: str) -> Step:
    span = {"start": "{start_instant}", "end": "{end_instant}"}
    request = {"filters": {"date": span, flag: True}, "count": SAMPLE_SESSIONS}
    args = {"api": "clarity", "method": "recordings", "request": request}
    why = "Optional: see which elements, in a sample of sessions."
    return Step(why, "query_api", args, read, ZERO_COUNT_STOP)


# Discovery makes no call, so the dates of a task cannot start on the first day of data of the
# property. core._gate_delta finds that day and gives the next dates in reason.
FIRST_DAY_READ = (
    "If a range starts before the first day of data of the property, delta is null, "
    "first_data_day names that day, and reason gives the pair to ask: both starts move forward "
    "by the same days to first_data_day. Ask that pair. If it is too short to show a trend, or "
    "reason says that no pair fits, ask 2 ranges of the same length that do not overlap and "
    "start on or after first_data_day, or report that the property has too little history. If "
    "reason says that the server could not check the first day, report the change as unknown."
)


def _search_step(sources: list[str], why: str, read: str, compare: bool = False) -> Step:
    args: dict[str, Any] = {
        "sources": sources,
        "page": "<page>",
        "start_date": "{start}",
        "end_date": "{end}",
    }
    if compare:
        args |= {"compare_start_date": "{compare_start}", "compare_end_date": "{compare_end}"}
        read = f"{read} {FIRST_DAY_READ}"
    return Step(why, "get_seo_data", args, read)


def _click_recipe(flag: str, question: str, count_read: str, event_read: str, means: str) -> Recipe:
    return Recipe(
        "clarity",
        (
            _question(question, "Get the count for each page.", count_read),
            _sessions(flag, event_read),
        ),
        means,
        (CLARITY_QUOTA, ZERO_COUNT_STOP, RECORDING_AGE, OTHER_HOSTS, COUNT_ONLY, SESSION_LINKS),
    )


EVENT_ROWS = (
    "In each row, read timeline[].url for the page and timeline[].timelineEvents[]. An event "
    "with eventtype {name} is 1 such click. text is the element text: masked, cut at 25 "
    "characters, and it can be empty. hash identifies the element on its page, so count by "
    "page and hash. url has no query and no fragment. The rows are the newest sessions first, "
    "and the server keeps only the fields in kept_fields. This is a sample: never use it as a "
    "total."
)

TASKS: dict[str, Task] = {
    "dead_clicks": Task(
        "Clicks with no response, for each page and element.",
        ("dead click", "deadclick", "dead tap", "click with no response"),
        (
            _click_recipe(
                "deadClickPresent",
                COUNTS["deadClickPresent"].question,
                "rows has 1 row for each page: VisitedUrl and TotalDeadClicks.",
                EVENT_ROWS.format(name='"Dead click"'),
                "A dead click is a click that made no change on the page. Many on 1 element "
                "mean that people expect it to do something: a link that is not a link, or a "
                "broken control.",
            ),
        ),
        days=CLICK_DAYS,
        max_days=RECORDING_DAYS,
    ),
    "rage_clicks": Task(
        "Rapid repeated clicks on 1 place, for each page and element.",
        ("rage click", "rageclick", "angry click", "repeated click"),
        (
            _click_recipe(
                "rageClickPresent",
                COUNTS["rageClickPresent"].question,
                "rows has 1 row for each page. This question is not yet proven on real data: "
                "read the columns that come back.",
                EVENT_ROWS.format(name='"Rage clicks"'),
                "A rage click is a fast series of clicks on 1 place. It shows frustration: "
                "a slow or a broken control.",
            ),
        ),
        days=CLICK_DAYS,
        max_days=RECORDING_DAYS,
    ),
    "quickback_clicks": Task(
        "Clicks to a page that people leave again at once.",
        ("quickback", "quick back", "quick return"),
        (
            Recipe(
                "clarity",
                (
                    _question(
                        COUNTS["quickbackClickPresent"].question,
                        "Get the count for each page.",
                        "rows has 1 row for each page. This question is not yet proven on "
                        "real data: read the columns that come back.",
                    ),
                    _sessions(
                        "quickbackClickPresent",
                        "The filter finds sessions with a quickback. No real timeline has "
                        "shown an eventtype for it yet, so read the eventtype values that "
                        "come back, and treat this as a sample.",
                    ),
                ),
                "A quickback is a click to a page, then a fast return. The page did not "
                "give what the link promised.",
                (
                    CLARITY_QUOTA,
                    ZERO_COUNT_STOP,
                    RECORDING_AGE,
                    OTHER_HOSTS,
                    COUNT_ONLY,
                    SESSION_LINKS,
                ),
            ),
        ),
        days=CLICK_DAYS,
        max_days=RECORDING_DAYS,
    ),
    "scroll_depth_by_page": Task(
        "How far people scroll on each page.",
        ("scroll depth", "scroll percent", "how far do people scroll", "how far people scroll"),
        (
            Recipe(
                "clarity",
                (
                    _question(
                        "Average scroll depth percentage per page",
                        "Get the average depth of each page.",
                        "rows has 1 row for each page: VisitedUrl and AvgScrollDepthPercent.",
                    ),
                ),
                "The average of the deepest point of each visit, as a percent of the page.",
                (CLARITY_QUOTA, OLD_DATA, OTHER_HOSTS, COUNT_ONLY),
            ),
            Recipe(
                "ga4",
                (
                    Step(
                        "Count the scroll events of each page.",
                        "query_api",
                        {
                            "api": "ga4",
                            "method": "run_report",
                            "request": {
                                "dateRanges": [
                                    {"startDate": "{start_ago}", "endDate": "{end_ago}"}
                                ],
                                "dimensions": [{"name": "pagePath"}],
                                "metrics": [{"name": "eventCount"}],
                                "dimensionFilter": {
                                    "filter": {
                                        "fieldName": "eventName",
                                        "stringFilter": {"value": "scroll"},
                                    }
                                },
                            },
                        },
                        "rows has pagePath and eventCount.",
                    ),
                ),
                "The default scroll event of GA4 fires 1 time when a visit reaches 90% of the "
                "page. So this counts deep visits, and it is not a depth.",
                ("Enhanced measurement must be on, or the report is empty.",),
            ),
        ),
    ),
    "most_clicked_elements": Task(
        "The elements that people click most, for each page.",
        ("most clicked", "clicked element", "click most", "top clicked", "popular element"),
        (
            Recipe(
                "clarity",
                (
                    _question(
                        "Most clicked elements per page",
                        "Get the top elements of each page.",
                        "rows has 1 row for each page: VisitedUrl and MostClickedElements, a "
                        "list of element text cut at 25 characters. The list can be empty.",
                    ),
                ),
                "The list has the text of each element, with no click count.",
                (CLARITY_QUOTA, OLD_DATA, OTHER_HOSTS, COUNT_ONLY),
            ),
        ),
    ),
    "page_over_two_periods": Task(
        "1 page in 2 time frames of the same length.",
        (
            "2 period",
            "two period",
            "period over period",
            "compare period",
            "month over month",
            "week over week",
        ),
        (
            Recipe(
                "search_console",
                (
                    _search_step(
                        ["search_pages"],
                        "Get clicks and impressions of the page in both periods.",
                        "comparison has the 2nd period. delta is main minus comparison.",
                        compare=True,
                    ),
                ),
                "A positive delta means that the newer period is higher.",
                ("Read thin_data before you claim a trend.",),
            ),
            Recipe(
                "ga4",
                (
                    _search_step(
                        ["ga4_landing_pages"],
                        "Get organic sessions that start on the page in both periods.",
                        "comparison has the 2nd period. delta is main minus comparison, and it "
                        "is empty when a period has no sessions.",
                        compare=True,
                    ),
                ),
                "A positive delta means that the newer period is higher.",
                ("Read thin_data before you claim a trend.",),
            ),
            Recipe(
                "clarity",
                (
                    Step(
                        "Get the sessions of the page in the newer period.",
                        "query_api",
                        {
                            "api": "clarity",
                            "method": "dashboard_query",
                            "request": {
                                "query": "Session count of page <page_url> between "
                                "{start_slash} and {end_slash}"
                            },
                        },
                        "rows has the count. Read answered_question for the dates.",
                    ),
                    Step(
                        "Get the sessions of the page in the older period.",
                        "query_api",
                        {
                            "api": "clarity",
                            "method": "dashboard_query",
                            "request": {
                                "query": "Session count of page <page_url> between "
                                "{compare_start_slash} and {compare_end_slash}"
                            },
                        },
                        "rows has the count. Read answered_question for the dates.",
                    ),
                ),
                "Subtract the 2 counts yourself: the server gives no delta for this path.",
                (CLARITY_QUOTA, OLD_DATA, COUNT_ONLY),
            ),
            Recipe(
                "bing",
                not_available="The Bing traffic methods take no date range, so 2 chosen "
                "periods are not possible.",
            ),
        ),
        days=SEARCH_DAYS,
        lag=SEARCH_LAG_DAYS,
        compare=True,
    ),
    "queries_of_page": Task(
        "The search queries that bring people to 1 page.",
        (
            "search query",
            "search term",
            "query of",
            "query for",
            "which query",
            "what query",
            "keyword for",
            "keyword of",
        ),
        (
            Recipe(
                "search_console",
                (
                    _search_step(
                        ["search_queries"],
                        "Get the top queries of the page.",
                        "rows has query, clicks, impressions, ctr, and position.",
                    ),
                ),
                "Google search only. Rare queries are hidden, so rows can sum to less than totals.",
                ("More than 25 rows: use search_analytics with a page filter.",),
            ),
            Recipe(
                "bing",
                (
                    Step(
                        "Get the top queries of the page.",
                        "query_api",
                        {
                            "api": "bing",
                            "method": "GetPageQueryStats",
                            "request": {"page": "<page_url>"},
                        },
                        "rows has Query, Clicks, Impressions, and the date of the week.",
                    ),
                ),
                "Bing search only. Bing updates this each week, and it takes no date range.",
            ),
            Recipe(
                "ga4",
                not_available="GA4 has no search query data: it knows the landing page, not "
                "the query.",
            ),
        ),
        days=SEARCH_DAYS,
        lag=SEARCH_LAG_DAYS,
    ),
    "core_web_vitals": Task(
        "Field and lab speed of 1 page.",
        ("web vital", "page speed", "pagespeed", "lighthouse", "field data", "site speed"),
        (
            Recipe(
                "crux",
                (
                    Step(
                        "Get the p75 field data of real Chrome users.",
                        "get_seo_data",
                        {"sources": ["chrome_field_data"], "page": "<page>"},
                        "rows has the p75 value and a rating for each metric.",
                    ),
                ),
                "Use field data over lab data. It covers a 28-day window.",
                ("A page with too little Chrome traffic has no data.",),
            ),
            Recipe(
                "pagespeed",
                (
                    Step(
                        "Run a live mobile Lighthouse test.",
                        "get_seo_data",
                        {"sources": ["page_speed"], "page": "<page>"},
                        "rows has scores, lab metrics, and opportunities.",
                    ),
                ),
                "A lab test: 1 run on a simulated device, not real users.",
            ),
        ),
    ),
}


def _words(text: str) -> str:
    tokens = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    singular = [
        token[:-3] + "y"
        if token.endswith("ies")
        else token[:-1]
        if token.endswith("s") and not token.endswith("ss")
        else token
        for token in tokens
    ]
    return f" {' '.join(singular)} "


def find(asked: str) -> list[str]:
    """Match whole phrases. A loose word match would give a wrong call with full confidence."""
    words = _words(asked)
    return [
        name
        for name, task in TASKS.items()
        if any(_words(phrase) in words for phrase in (name, *task.asks))
    ]


def _ago(days: int) -> str:
    return "today" if days == 0 else f"{days}daysAgo"


def _dated(task: Task) -> bool:
    return "{start" in str([step.args for recipe in task.recipes for step in recipe.steps])


def _length(name: str, task: Task, start: date, end: date) -> int:
    if not _dated(task):
        raise params.BadInput(f"{name} has no dates, so it takes no start_date.")
    if start > end:
        raise params.BadInput(f"start_date must not be after {end}, the end of {name}.")
    days = (end - start).days + 1
    if task.max_days and days > task.max_days:
        first = end - timedelta(days=task.max_days - 1)
        raise params.BadInput(
            f"start_date of {name} must be on or after {first}: Clarity keeps recordings for "
            f"{task.max_days} days."
        )
    return days


def _marks(
    name: str, task: Task, today: date, given: date | None
) -> tuple[dict[str, str], dict[str, Any]]:
    lag = task.lag + params.SLACK_DAYS if task.lag else 0
    end = today - timedelta(days=lag)
    length = _length(name, task, given, end) if given else task.days
    start = end - timedelta(days=length - 1)
    days = {"start": start, "end": end}
    window: dict[str, Any] = {"start": str(start), "end": str(end), "days": length}
    if task.compare:
        days["compare_end"] = start - timedelta(days=1)
        days["compare_start"] = days["compare_end"] - timedelta(days=length - 1)
        window |= {name: str(days[name]) for name in ("compare_start", "compare_end")}
    marks = {}
    for name, day in days.items():
        marks[f"{{{name}}}"] = str(day)
        marks[f"{{{name}_slash}}"] = day.strftime("%Y/%m/%d")
    marks["{start_instant}"] = f"{start}T00:00:00.000Z"
    marks["{end_instant}"] = f"{end}T23:59:59.999Z"
    # GA4 reads a relative date in the property zone, so it needs no slack. A given start is
    # the day that the agent names.
    marks["{start_ago}"] = _ago((today - given).days if given else task.lag + task.days - 1)
    marks["{end_ago}"] = _ago(task.lag)
    return marks, window


def _fill(value: Any, marks: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _fill(item, marks) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, marks) for item in value]
    if isinstance(value, str):
        for mark, real in marks.items():
            value = value.replace(mark, real)
    return value


def _step(step: Step, site: str, marks: dict[str, str]) -> dict[str, Any]:
    args = _fill(step.args, marks)
    row = {
        "why": step.why,
        "tool": step.tool,
        "args": {"site": site, **args} if site else args,
        "read": step.read,
    }
    return {**row, "skip_if": step.skip_if} if step.skip_if else row


def _row(name: str, recipe: Recipe, site: str, marks: dict[str, str]) -> dict[str, Any]:
    steps = [_step(step, site, marks) for step in recipe.steps]
    return {
        "task": name,
        "api": recipe.api,
        "steps": steps,
        "needs": sorted(set(INPUT_MARK.findall(str([step["args"] for step in steps])))),
        "means": recipe.means,
        "limits": list(recipe.limits),
        "not_available": recipe.not_available or None,
    }


def build(
    name: str, site: str, api: str, today: date | None = None, start: date | None = None
) -> dict[str, Any]:
    """start is the first day that the agent wants, such as the day of a new page design."""
    task = TASKS[name]
    marks, window = _marks(name, task, today or params.today(), start)
    recipes = [recipe for recipe in task.recipes if api in ("", recipe.api)]
    return {"window": window, "rows": [_row(name, item, site, marks) for item in recipes]}


def summaries() -> list[dict[str, str]]:
    return [{"task": name, "summary": task.summary} for name, task in TASKS.items()]


def describe() -> str:
    """Build the list for the tool description from TASKS, so that it cannot drift."""
    return "\n".join(f"- {name}: {task.summary}" for name, task in TASKS.items())
