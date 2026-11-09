"""An agent asks for a thing by name and gets the call. Each call here runs for real."""

import json
import re
from datetime import date, timedelta
from pathlib import Path

import httpx2
import pytest

from seo_mcp import core, params, providers, server, tasks
from test_query_api import HOST, SITE, answer

README = Path(__file__).parent.parent / "README.md"
FILLED = {"<page>": "/a", "<page_url>": f"https://{HOST}/a"}
STEPS = [
    pytest.param(task, recipe.api, at, step, id=f"{task}-{recipe.api}-{at}")
    for task, entry in tasks.TASKS.items()
    for recipe in entry.recipes
    for at, step in enumerate(recipe.steps)
]


@pytest.fixture
def live(write_config, fake_net, account, monkeypatch):
    monkeypatch.setenv("CLARITY_ENV", "clarity-token-value-1234")
    monkeypatch.setenv("GOOGLE_KEY_ENV", "google-key-value-1234")
    monkeypatch.setenv("BING_KEY_ENV", "bing-key-value-5678")
    shared = {
        **account,
        "google_api_key": {"env": "GOOGLE_KEY_ENV"},
        "bing_api_key": {"env": "BING_KEY_ENV"},
    }
    write_config(SITE, shared)
    return fake_net(answer)


def fill(value):
    text = json.dumps(value)
    for mark, real in FILLED.items():
        text = text.replace(mark, real)
    return json.loads(text)


@pytest.mark.parametrize(
    ("asked", "name"),
    [
        ("dead clicks", "dead_clicks"),
        ("dead_clicks", "dead_clicks"),
        ("Dead-Click", "dead_clicks"),
        ("deadclicks", "dead_clicks"),
        ("which pages have dead clicks", "dead_clicks"),
        ("rage clicks", "rage_clicks"),
        ("scroll depth by page", "scroll_depth_by_page"),
        ("how far do people scroll", "scroll_depth_by_page"),
        ("most clicked elements", "most_clicked_elements"),
        ("what do people click most", "most_clicked_elements"),
        ("quickback clicks", "quickback_clicks"),
        ("quick back", "quickback_clicks"),
        ("1 page over 2 periods", "page_over_two_periods"),
        ("compare a page between two periods", "page_over_two_periods"),
        ("search queries of 1 page", "queries_of_page"),
        ("which queries bring people to this page", "queries_of_page"),
        ("core web vitals", "core_web_vitals"),
    ],
)
async def test_an_ask_finds_its_task(asked, name):
    data = await core.query_api(task=asked)
    assert data["status"] == "ok"
    assert {row["task"] for row in data["rows"]} == {name}


async def test_an_ask_can_find_2_tasks():
    data = await core.query_api(task="dead clicks and rage clicks")
    assert {row["task"] for row in data["rows"]} == {"dead_clicks", "rage_clicks"}


@pytest.mark.parametrize("asked", ["the weather", "click", " ", "page"])
async def test_an_unknown_ask_lists_the_tasks_and_guesses_no_call(asked):
    data = await core.query_api(task=asked)
    assert data["status"] == "unsupported"
    assert data["accepts"] == sorted(tasks.TASKS)
    assert data["rows"] == []


async def test_discovery_needs_no_network_and_no_sites_file(fake_net, monkeypatch, tmp_path):
    monkeypatch.setenv("SEO_MCP_CONFIG", str(tmp_path / "missing.json"))
    calls = fake_net(answer)
    for name in tasks.TASKS:
        assert (await core.query_api(task=name))["status"] == "ok"
    assert (await core.query_api())["tasks"]
    assert not calls


async def test_a_task_goes_with_no_method_and_no_request():
    for extra in ({"method": "recordings"}, {"request": {"count": 1}}):
        data = await core.query_api(api="clarity", task="dead clicks", **extra)
        assert data["status"] == "error"
        assert "task" in data["reason"]


async def test_api_keeps_1_provider_of_a_task():
    data = await core.query_api(api="ga4", task="scroll depth")
    assert [row["api"] for row in data["rows"]] == ["ga4"]
    wrong = await core.query_api(api="bing", task="dead clicks")
    assert wrong["status"] == "unsupported"
    assert await core.query_api(api="nope", task="dead clicks") == await core.query_api(api="nope")


async def test_dead_clicks_ask_the_count_first_then_the_elements():
    data = await core.query_api(site=HOST, task="which pages have dead clicks")
    (row,) = data["rows"]
    first, second = row["steps"]
    assert first["args"]["method"] == "dashboard_query"
    assert first["args"]["site"] == HOST
    assert "Dead click count per page between" in first["args"]["request"]["query"]
    assert "TotalDeadClicks" in first["read"]
    assert second["args"]["method"] == "recordings"
    assert second["args"]["request"]["filters"]["deadClickPresent"] is True
    assert '"Dead click"' in second["read"]
    assert "sample" in second["read"]
    assert not row["needs"]


@pytest.mark.parametrize("name", ["dead_clicks", "rage_clicks", "quickback_clicks"])
def test_a_click_recipe_looks_back_28_days_and_asks_20_sessions(name):
    filled = tasks.build(name, "", "", today=date(2026, 11, 6))
    assert filled["window"] == {"start": "2026-10-10", "end": "2026-11-06", "days": 28}
    steps = filled["rows"][0]["steps"]
    assert "2026/10/10 and 2026/11/06" in steps[0]["args"]["request"]["query"]
    request = steps[1]["args"]["request"]
    span = request["filters"]["date"]
    assert span == {"start": "2026-10-10T00:00:00.000Z", "end": "2026-11-06T23:59:59.999Z"}
    assert request["count"] == 20


@pytest.mark.parametrize("name", ["scroll_depth_by_page", "most_clicked_elements"])
def test_a_dashboard_only_clarity_task_keeps_90_days(name):
    filled = tasks.build(name, "", "clarity", today=date(2026, 11, 6))
    assert filled["window"] == {"start": "2026-08-09", "end": "2026-11-06", "days": 90}


def test_a_start_date_from_the_agent_sets_both_click_steps():
    filled = tasks.build("dead_clicks", "", "", today=date(2026, 11, 6), start=date(2026, 10, 30))
    assert filled["window"] == {"start": "2026-10-30", "end": "2026-11-06", "days": 8}
    first, second = filled["rows"][0]["steps"]
    assert "2026/10/30 and 2026/11/06" in first["args"]["request"]["query"]
    span = second["args"]["request"]["filters"]["date"]
    assert span == {"start": "2026-10-30T00:00:00.000Z", "end": "2026-11-06T23:59:59.999Z"}


def test_a_start_date_keeps_2_periods_of_the_same_length():
    filled = tasks.build(
        "page_over_two_periods", "", "", today=date(2026, 11, 6), start=date(2026, 10, 27)
    )
    assert filled["window"] == {
        "start": "2026-10-27",
        "end": "2026-11-02",
        "days": 7,
        "compare_start": "2026-10-20",
        "compare_end": "2026-10-26",
    }


def test_a_start_date_gives_ga4_that_start_as_a_plain_date():
    filled = tasks.build(
        "scroll_depth_by_page", "", "ga4", today=date(2026, 11, 6), start=date(2026, 10, 27)
    )
    ranges = filled["rows"][0]["steps"][0]["args"]["request"]["dateRanges"]
    assert ranges == [{"startDate": "2026-10-27", "endDate": "2026-11-05"}]


@pytest.mark.parametrize(
    ("name", "start", "named"),
    [
        pytest.param("dead_clicks", date(2026, 11, 7), "after", id="after-the-end"),
        pytest.param("dead_clicks", date(2026, 10, 7), "2026-10-08", id="past-recording-retention"),
        pytest.param("page_over_two_periods", date(2026, 11, 3), "after", id="after-a-lagged-end"),
        pytest.param("core_web_vitals", date(2026, 11, 1), "no dates", id="task-with-no-dates"),
    ],
)
def test_a_start_date_that_cannot_apply_is_refused(name, start, named):
    with pytest.raises(params.BadInput, match=named):
        tasks.build(name, "", "", today=date(2026, 11, 6), start=start)


def test_a_click_recipe_takes_a_start_at_the_retention_edge():
    filled = tasks.build("dead_clicks", "", "", today=date(2026, 11, 6), start=date(2026, 10, 8))
    assert filled["window"]["days"] == 30


async def test_query_api_passes_the_start_date_to_the_task():
    start = (params.today() - timedelta(days=4)).isoformat()
    data = await core.query_api(task="dead clicks", start_date=start)
    assert data["status"] == "ok"
    assert data["rows"][0]["window"]["start"] == start


@pytest.mark.parametrize(
    ("args", "named"),
    [
        pytest.param({"task": "dead clicks", "start_date": "2026-13-01"}, "YYYY-MM-DD", id="bad"),
        pytest.param({"api": "clarity", "start_date": "2026-11-01"}, "task", id="no-task"),
    ],
)
async def test_query_api_refuses_a_start_date_that_it_cannot_use(args, named):
    data = await core.query_api(**args)
    assert data["status"] == "error"
    assert named in data["reason"]


def test_2_periods_have_the_same_length_and_do_not_overlap():
    filled = tasks.build("page_over_two_periods", "", "", today=date(2026, 11, 6))
    assert filled["window"] == {
        "start": "2026-10-06",
        "end": "2026-11-02",
        "days": 28,
        "compare_start": "2026-09-08",
        "compare_end": "2026-10-05",
    }


@pytest.mark.parametrize("today", [date(2026, 3, 18), date(2026, 3, 19)])
def test_a_task_end_with_a_lag_is_complete_in_each_zone(today):
    task = tasks.TASKS["page_over_two_periods"]
    end = date.fromisoformat(
        tasks.build("page_over_two_periods", "", "", today=today)["window"]["end"]
    )
    # The day of a place is the UTC date, the day before, or the day after.
    for place in (today - timedelta(days=1), today, today + timedelta(days=1)):
        assert end <= place - timedelta(days=task.lag), place


def test_a_ga4_task_step_sends_plain_dates_that_end_yesterday():
    built = [
        tasks.build("scroll_depth_by_page", "", "ga4", today=day)
        for day in (date(2026, 3, 18), date(2026, 3, 19))
    ]
    ranges = [
        row["steps"][0]["args"]["request"]["dateRanges"] for item in built for row in item["rows"]
    ]
    assert ranges == [
        [{"startDate": "2025-12-19", "endDate": "2026-03-17"}],
        [{"startDate": "2025-12-20", "endDate": "2026-03-18"}],
    ]


async def test_a_step_names_the_input_that_it_needs():
    data = await core.query_api(task="queries_of_page")
    needs = {row["api"]: row["needs"] for row in data["rows"]}
    assert needs == {"search_console": ["<page>"], "bing": ["<page_url>"], "ga4": []}


async def test_a_provider_with_no_answer_says_why_and_gives_no_call():
    data = await core.query_api(task="queries_of_page")
    (row,) = [row for row in data["rows"] if row["api"] == "ga4"]
    assert row["steps"] == []
    assert "search quer" in row["not_available"]


# The catalog cannot drift: each step runs through the public tools, with only the network fake.
@pytest.mark.parametrize(("task", "api", "at", "step"), STEPS)
async def test_each_step_runs_through_the_real_tool(live, task, api, at, step):
    built = tasks.build(task, HOST, api, today=date.today())
    sent_step = next(row for row in built["rows"] if row["api"] == api)["steps"][at]
    args = fill(sent_step["args"])
    assert sent_step["tool"] in {"query_api", "get_seo_data"}
    run = core.query_api if sent_step["tool"] == "query_api" else core.get_seo_data
    data = await run(**args)
    assert data["status"] == "ok", data["reason"]
    for name, result in data.get("sources", {}).items():
        assert result["status"] == "ok", (name, result["reason"])
    assert live


def test_each_step_of_get_seo_data_names_its_sources():
    for _, _, _, step in (param.values for param in STEPS):
        if step.tool == "get_seo_data":
            assert step.args["sources"], "A call with no sources runs every source."


def test_each_api_has_a_task():
    covered = {recipe.api for entry in tasks.TASKS.values() for recipe in entry.recipes}
    assert set(providers.APIS) <= covered
    assert covered <= set(providers.APIS)


def test_each_clarity_recipe_warns_about_the_quota():
    for entry in tasks.TASKS.values():
        for recipe in entry.recipes:
            if recipe.api == "clarity":
                assert tasks.CLARITY_QUOTA in recipe.limits


def test_tool_description_and_readme_list_each_task():
    readme = README.read_text()
    for name in tasks.TASKS:
        assert name in server.QUERY_API
        assert f"`{name}`" in readme


def test_the_server_instructions_point_to_tasks():
    assert "task" in server.server.instructions


def sampled_recipes():
    """Each recipe that asks the dashboard for a count, then spends a call on a sample."""
    for name, entry in tasks.TASKS.items():
        for recipe in entry.recipes:
            methods = [step.args.get("method") for step in recipe.steps]
            if "dashboard_query" in methods and "recordings" in methods:
                yield name, recipe


def test_the_click_tasks_keep_2_steps_with_the_dashboard_first():
    found = {name: recipe for name, recipe in sampled_recipes()}
    assert set(found) == {"dead_clicks", "rage_clicks", "quickback_clicks"}
    for recipe in found.values():
        methods = [step.args["method"] for step in recipe.steps]
        assert methods == ["dashboard_query", "recordings"]


async def test_a_count_of_0_stops_the_sample_step():
    for name, recipe in sampled_recipes():
        assert tasks.ZERO_COUNT_STOP in recipe.limits, name
        data = await core.query_api(api="clarity", task=name)
        first, second = data["rows"][0]["steps"]
        assert "skip_if" not in first
        assert second["skip_if"] == tasks.ZERO_COUNT_STOP
        assert "do not call recordings" in tasks.ZERO_COUNT_STOP


def test_the_readme_states_the_rule_of_a_count_of_0():
    assert "do not call `recordings`" in README.read_text()


# The column and the words of each click count, as a real dashboard answer names them.
COUNTS = {
    "dead_clicks": ("TotalDeadClicks", "dead click"),
    "rage_clicks": ("TotalRageClicks", "rage click"),
    "quickback_clicks": ("TotalQuickbackClicks", "quickback click"),
}


def rewritten(name, window, start=None, end=None):
    """The question as Clarity rewrites it, from a real answer."""
    first = (start or window["start"]).replace("-", "/")
    last = (end or window["end"]).replace("-", "/")
    words = COUNTS[name][1]
    return (
        f"Total {words} counts per visited page for non-bot users between "
        f"'{first} 00:00:00' and '{last} 23:59:59', showing top 10 pages by {words}s."
    )


def dashboard(fake_net, rows, question):
    def reply(request):
        if request.url.path.endswith("/mcp/dashboard/query"):
            return httpx2.Response(200, json={"data": rows, "query": question})
        if request.url.path.endswith("/mcp/recordings/sample"):
            return httpx2.Response(200, json=[])
        return answer(request)

    return fake_net(reply)


def clarity_paths(calls):
    return [call.url.path for call in calls if "clarity" in call.url.host]


async def run_recipe(name, fake_net, rows=None, question=None, start=""):
    """Run the 2 steps of the task as an agent that ignores skip_if."""
    data = await core.query_api(HOST, "clarity", "", None, name, start)
    column = COUNTS[name][0]
    window = data["rows"][0]["window"]
    rows = [{"VisitedUrl": f"https://{HOST}/a", column: 0}] if rows is None else rows
    calls = dashboard(fake_net, rows, question or rewritten(name, window))
    first, second = data["rows"][0]["steps"]
    count = await core.query_api(**first["args"])
    sample = await core.query_api(**second["args"])
    return count, sample, calls


@pytest.mark.parametrize("name", sorted(COUNTS))
async def test_a_0_count_makes_the_server_skip_the_sample(live, fake_net, name):
    count, sample, calls = await run_recipe(name, fake_net)
    assert count["status"] == "ok"
    assert count["calls_used_today"] == 1
    assert sample["status"] == "skipped", (
        f"{name}: the server sent the sample after a 0 count. In src/seo_mcp/tasks.py, build "
        "the dashboard question of the recipe from COUNTS, so that clarity._zero_count finds it."
    )
    assert "0" in sample["reason"]
    assert clarity_paths(calls) == ["/mcp/dashboard/query"]
    assert sample["calls_used_today"] == 1
    assert sample["rows"] == []


@pytest.mark.parametrize("name", sorted(COUNTS))
async def test_a_0_count_skips_the_sample_also_from_a_start_date(live, fake_net, name):
    start = (params.today() - timedelta(days=6)).isoformat()
    count, sample, calls = await run_recipe(name, fake_net, start=start)
    assert f"between {start.replace('-', '/')} and" in count["applied"]["query"]
    assert sample["status"] == "skipped"
    assert clarity_paths(calls) == ["/mcp/dashboard/query"]


def test_each_click_task_has_a_count_that_the_server_reads():
    """A new click task must name its count, so that the server can enforce skip_if."""
    assert {name for name, _ in sampled_recipes()} == set(COUNTS), (
        "Each click task needs a row in tasks.COUNTS and in COUNTS of tests/test_tasks.py."
    )


async def test_a_count_above_0_sends_the_sample(live, fake_net):
    rows = [
        {"VisitedUrl": f"https://{HOST}/a", "TotalDeadClicks": 3},
        {"VisitedUrl": f"https://{HOST}/b", "TotalDeadClicks": 0},
    ]
    _, sample, calls = await run_recipe("dead_clicks", fake_net, rows)
    assert sample["status"] == "ok"
    assert clarity_paths(calls) == ["/mcp/dashboard/query", "/mcp/recordings/sample"]
    assert sample["calls_used_today"] == 2


async def test_no_saved_count_sends_the_sample(live, fake_net):
    data = await core.query_api(HOST, "clarity", "", None, "dead_clicks")
    calls = dashboard(fake_net, [], "")
    sample = await core.query_api(**data["rows"][0]["steps"][1]["args"])
    assert sample["status"] == "ok"
    assert clarity_paths(calls) == ["/mcp/recordings/sample"]
    assert sample["calls_used_today"] == 1


ZERO = [{"VisitedUrl": f"https://{HOST}/a", "TotalDeadClicks": 0}]


@pytest.mark.parametrize(
    ("rows", "question"),
    [
        pytest.param([], None, id="no-rows"),
        pytest.param([{"VisitedUrl": "/a", "PageViews": 0}], None, id="no-count-column"),
        pytest.param([{"VisitedUrl": "/a", "TotalDeadClicks": None}], None, id="null-count"),
        pytest.param([{"VisitedUrl": "/a", "TotalDeadClicks": "1e-400"}], None, id="tiny-text"),
        pytest.param([{"VisitedUrl": "/a", "TotalDeadClicks": 0.0}], None, id="float"),
        pytest.param([{"VisitedUrl": "/a", "TotalDeadClicks": False}], None, id="bool"),
        pytest.param([{"TotalDeadClicks": 0, "DeadClickSessions": 0}], None, id="2-count-columns"),
        pytest.param(ZERO, "Total dead click counts per visited page", id="no-dates"),
        pytest.param(
            ZERO,
            "Total rage click counts between '{start} 00:00:00' and '{end} 23:59:59'",
            id="other-signal",
        ),
        pytest.param(
            ZERO,
            "Total dead click counts between '2026/01/01 00:00:00' and '{end} 23:59:59'",
            id="other-start",
        ),
        pytest.param(
            ZERO,
            "Total dead click counts between '{start} 00:00:00' and "
            "'{end} 23:59:59', showing top 10 pages by sessions.",
            id="top-by-other",
        ),
    ],
)
async def test_an_uncertain_0_still_sends_the_sample(live, fake_net, rows, question):
    """A wrong skip hides data, so each doubt sends the sample as before."""
    data = await core.query_api(HOST, "clarity", "", None, "dead_clicks")
    start, end = (data["rows"][0]["window"][key].replace("-", "/") for key in ("start", "end"))
    asked = question.format(start=start, end=end) if question else None
    _, sample, calls = await run_recipe("dead_clicks", fake_net, rows, asked)
    assert sample["status"] == "ok"
    assert clarity_paths(calls)[-1] == "/mcp/recordings/sample"


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"visitedUrls": [{"url": "/a", "operator": "contains"}]}, id="extra-filter"),
        pytest.param({"deadClickPresent": False}, id="flag-false"),
        pytest.param({"rageClickPresent": True}, id="other-flag"),
    ],
)
async def test_a_sample_of_another_question_still_goes_out(live, fake_net, change):
    data = await core.query_api(HOST, "clarity", "", None, "dead_clicks")
    calls = dashboard(fake_net, ZERO, rewritten("dead_clicks", data["rows"][0]["window"]))
    first, second = data["rows"][0]["steps"]
    await core.query_api(**first["args"])
    args = json.loads(json.dumps(second["args"]))
    filters = args["request"]["filters"]
    filters.pop("deadClickPresent")
    filters |= change
    sample = await core.query_api(**args)
    assert sample["status"] == "ok"
    assert clarity_paths(calls)[-1] == "/mcp/recordings/sample"


async def test_a_partial_day_window_still_sends_the_sample(live, fake_net):
    data = await core.query_api(HOST, "clarity", "", None, "dead_clicks")
    calls = dashboard(fake_net, ZERO, rewritten("dead_clicks", data["rows"][0]["window"]))
    first, second = data["rows"][0]["steps"]
    await core.query_api(**first["args"])
    args = json.loads(json.dumps(second["args"]))
    span = args["request"]["filters"]["date"]
    span["end"] = span["end"].replace("23:59:59.999Z", "12:00:00.000Z")
    sample = await core.query_api(**args)
    assert sample["status"] == "ok"
    assert clarity_paths(calls)[-1] == "/mcp/recordings/sample"


# The site gets new content each day, such as a blog post. A reset of the window on each change
# leaves too few days to show a click, so only a change to the design of the studied page counts.
DESIGN_CHANGE = (
    "Set start_date only when the layout or the elements of the studied page changed, such as a "
    "moved button or a new form. New content on other pages, such as a new blog post, does not "
    "reset the window."
)
# Text that tells an agent to reset the window on any change to the site.
ANY_CHANGE = re.compile(r"change to the site|day of the (last )?change\b", re.IGNORECASE)


def flat(text: str) -> str:
    """Joins the parts of a split Python string and the lines of a paragraph."""
    return " ".join(re.sub(r'"\s*\n\s*f?"', "", text).split())


async def test_a_click_task_resets_its_window_only_after_a_design_change():
    for name, _ in sampled_recipes():
        data = await core.query_api(task=name)
        limits = data["rows"][0]["limits"]
        assert any(DESIGN_CHANGE in limit for limit in limits), name
    assert DESIGN_CHANGE in flat(server.QUERY_API)


def test_no_text_resets_the_window_on_any_change_to_the_site():
    root = README.parent
    files = [README, root / "CHANGELOG.md", *sorted((root / "src").rglob("*.py"))]
    for path in files:
        found = ANY_CHANGE.search(flat(path.read_text()))
        assert not found, (
            f"{path.relative_to(root)} says {found.group(0)!r}: an agent resets start_date only "
            "after a design change of the studied page. Use the text of DESIGN_CHANGE."
        )
