---- MODULE agent_flow ----
\* The discovery steps of #20 and #23. The tool text names each API, query_api(api=X)
\* lists the fields (GA4: the standard dimensions and metrics), and metadata is a real request.
\* The agent side, from live-data.md and the task recipes. The agent has a real choice at the
\* stop rule, and the server enforces the rule when the agent ignores it.
EXTENDS Naturals
VARIABLES pc, count, spent, sample, netCalls
vars == <<pc, count, spent, sample, netCalls>>
Ends == {"noSite", "gapReported", "reported", "zeroReported"}
Discovery == {"question", "sites", "quotaRead", "choosing", "taskRows", "noTask", "fields"}

Init == pc = "question" /\ count = "unknown" /\ spent = 0 /\ sample = FALSE /\ netCalls = 0

Keep == UNCHANGED <<count, spent, sample, netCalls>>
ListSites == pc = "question" /\ pc' = "sites" /\ Keep
NoRow == pc = "sites" /\ pc' = "noSite" /\ Keep
ReadQuota == pc = "sites" /\ pc' = "quotaRead" /\ Keep
TooFewCalls == pc = "quotaRead" /\ pc' = "gapReported" /\ Keep
EnoughCalls == pc = "quotaRead" /\ pc' = "choosing" /\ Keep
PickSimple ==
  /\ pc = "choosing" /\ pc' = "simple" /\ netCalls' = netCalls + 1
  /\ UNCHANGED <<count, spent, sample>>
PickSpecific ==
  /\ pc = "choosing" /\ pc' = "specific" /\ netCalls' = netCalls + 1
  /\ UNCHANGED <<count, spent, sample>>
PickTask == pc = "choosing" /\ pc' = "taskRows" /\ Keep
\* server.QUERY_API names the 6 APIs, so the agent asks for the fields of 1 API with no catalog call.
AskFields == pc = "choosing" /\ pc' = "fields" /\ Keep
\* core._options: the fields, values, and (GA4) the standard names, with no network call.
FieldsEnough ==
  /\ pc = "fields" /\ pc' = "specific" /\ netCalls' = netCalls + 1
  /\ UNCHANGED <<count, spent, sample>>
\* ga4._metadata is a real API request: only for a custom name or a name that the list does not hold.
CustomName ==
  /\ pc = "fields" /\ pc' = "metadata" /\ netCalls' = netCalls + 1
  /\ UNCHANGED <<count, spent, sample>>
NamesRead ==
  /\ pc = "metadata" /\ pc' = "specific" /\ netCalls' = netCalls + 1
  /\ UNCHANGED <<count, spent, sample>>
NoTaskMatch == pc = "taskRows" /\ pc' = "noTask" /\ Keep
AnotherWay == pc = "noTask" /\ pc' = "choosing" /\ Keep
SimpleAnswer == pc = "simple" /\ pc' = "answerRead" /\ Keep
SimpleUnsupported == pc = "simple" /\ pc' = "choosing" /\ Keep
SpecificAnswer == pc = "specific" /\ pc' = "answerRead" /\ Keep
AskDashboard ==
  /\ pc = "taskRows" /\ pc' = "dashboard"
  /\ spent' = spent + 1 /\ netCalls' = netCalls + 1
  /\ UNCHANGED <<count, sample>>
\* count = "zero" is a 0 that the server can match: a 0 it cannot match is CountAbove here.
CountZero == pc = "dashboard" /\ pc' = "zeroCount" /\ count' = "zero" /\ UNCHANGED <<spent, sample, netCalls>>
CountAbove == pc = "dashboard" /\ pc' = "someCount" /\ count' = "above" /\ UNCHANGED <<spent, sample, netCalls>>
DashboardFails == pc = "dashboard" /\ pc' = "gapReported" /\ Keep
StopAtZero == pc = "zeroCount" /\ pc' = "zeroReported" /\ Keep
\* The agent ignores skip_if. clarity._zero_count reads the saved 0 count of the same click and
\* days, so recordings gives status skipped: no request, no spend, and no sample.
IgnoreStopRule == pc = "zeroCount" /\ pc' = "zeroReported" /\ Keep
AskSample ==
  /\ pc = "someCount" /\ pc' = "recordings"
  /\ spent' = spent + 1 /\ sample' = TRUE /\ netCalls' = netCalls + 1
  /\ UNCHANGED count
CountIsEnough == pc = "someCount" /\ pc' = "answerRead" /\ Keep
SampleAnswer == pc = "recordings" /\ pc' = "answerRead" /\ Keep
Report == pc = "answerRead" /\ pc' = "reported" /\ Keep
Done == pc \in Ends /\ UNCHANGED vars

Next ==
  ListSites \/ NoRow \/ ReadQuota \/ TooFewCalls \/ EnoughCalls \/ PickSimple
  \/ PickSpecific \/ PickTask \/ AskFields \/ FieldsEnough \/ CustomName \/ NamesRead \/ NoTaskMatch \/ AnotherWay \/ SimpleAnswer
  \/ SimpleUnsupported \/ SpecificAnswer \/ AskDashboard \/ CountZero \/ CountAbove
  \/ DashboardFails \/ StopAtZero \/ IgnoreStopRule \/ AskSample \/ CountIsEnough
  \/ SampleAnswer \/ Report \/ Done

\* The loop back to the choice stops after a few calls.
Bound == netCalls <= 3

Spec == Init /\ [][Next]_vars

\* After a 0 count, the agent makes no sample call.
NoSampleAfterZeroCount == ~(count = "zero" /\ sample)
\* The recipe asks the dashboard first.
DashboardComesFirst == sample => count # "unknown"
\* Discovery makes no network call.
\* A step rule: a loop back to the choice comes after real calls, so a state rule cannot tell
\* a discovery call from an earlier call.
DiscoverySpendsNothing ==
  [][(pc \in Discovery /\ pc' \in Discovery) => UNCHANGED <<spent, netCalls>>]_vars
====
