# Zepp app UI notes (for the future wearable-events dashboard)

Running notes on page layouts/UX patterns from walking through the
Zepp app, kept separate from the parser's own data-confirmation
notes in `parser/amazfit/README.md` (that's about the underlying
data; this one's about UI design). This file is for
later reference once we actually start building the mobile dashboard,
so we're not relying on conversation history/context for design
details by then.

Each entry: what's on the page, how it's laid out, what data it'd need
(cross-reference `parser/amazfit/README.md` if the data isn't confirmed yet),
and a rough buildable-now assessment.

## General navigation

- Top-level bottom nav: Home / Workout / Badges / Aura / Device.
  **Aura is a paid-subscription upsell (AI insights/reports) - skip
  entirely, not a real data source.**
- The "Overview"/Home tab's card list (which metrics show, in what
  order) is **user-customizable** in the real app (pencil/edit icons
  next to "CORE METRICS" and the ring chart). The specific cards
  captured in these notes are just the ones currently chosen, not a
  fixed set - worth keeping our own version similarly
  configurable/orderable rather than a fixed layout, if feasible.
- Common detail-page pattern: back arrow + all-caps title in a header
  bar, then a stack of white cards on a light-gray background. Several
  pages share a `D / W / M / Y` segmented control near the top to
  switch the time range - a reusable component if we build this.
- Very common card pattern: big number + qualitative label (colored:
  green="Good", presumably other colors for other tiers) + a
  horizontal range/gauge bar underneath, sometimes with a "Baseline"
  or goal marker. The "Baseline --" state (baseline not yet
  established) recurs across multiple metrics independently (seen on
  both HRV and Resting Heart Rate so far) - likely a shared
  personal-baseline component, not a one-off, and likely needs some
  minimum history length before it populates on any metric that uses
  it.
- Very common second pattern: "Last 7 days" bar or scatter chart,
  small, appears on almost every detail page.

## Page: Overview / Home (Today tab)

- Ring chart (steps/sleep/exertion combined, multi-colored concentric
  arcs) + "Review Insight" prompt (AI feature, presumably Aura-linked -
  skip).
- Activities card: most recent workout entry (name, duration, avg HR,
  calories) - links to workout summary data.
- Core Metrics: user-configurable list of metric cards (Sleep
  Duration, HRV, Sleep Regularity, etc. currently selected) - each
  links to its own detail page (see below).
- Heart Health card: current BPM + a colored zone gauge (relaxed/
  light/etc.) + a composite "RHR, SpO2, HRV - within range" status
  line.
- Steps card: count, progress bar, distance, calories.
- Stress card: current value + qualitative label + a small tick-mark
  timeline.

## Page: Sleep Duration (linked from Overview)

- Big number: total sleep duration (e.g. "9hr 1min") + qualitative
  label ("Good").
- Horizontal range bar: 0 to goal duration, with a marker for today's
  value (marker can sit past the end of the bar if duration exceeds
  goal, as it did here).
- "Goal 8hr 20min >" - tappable, links to a goal-setting page (not
  explored yet).
- "Last 7 days" bar chart (one bar per day, hours).
- "Compared with other users" - a percentile ("longer than 88% of
  users") + a population distribution histogram with the user's
  position marked. **NOT reproducible** - this needs Zepp's own
  cross-user cloud aggregate, which we have no access to and never
  will. If we want a "compared to..." feature, it'd have to be framed
  differently (e.g. compared to the person's own trailing average, or
  a household member via wearable-events' existing multi-user
  support) rather than attempting to mimic this exactly.
- "How Much Sleep Do I Need?" - static educational blurb, not
  user data, skip.
- **Buildable now**: yes, using `sleep_session_duration_s` (already
  extracted for both devices, Colmi has its own equivalent field) -
  everything except the cross-user comparison and the goal-setting
  flow.

## Page: Heart Rate Variability (linked from Overview)

- `D / W / M / Y` time-range selector.
- Big number: HRV in ms + qualitative label + a "Lower - Baseline -
  Higher" gauge (baseline showed `--`, presumably needs more history
  to establish - same situation as Sleep Regularity below).
- **"Sleep Timeline" chart** (mislabeled - it's actually a jagged
  line graph of HRV values *throughout the night*, not a sleep-stage
  timeline despite the name) - min/max annotated, spans the whole
  sleep window (00:02-09:14 in this example). This is genuinely
  important: it confirms Zepp's own per-night HRV detail comes from
  multiple readings across the night, not one summary number - which
  matches exactly what we already extract
  (`GENERIC_HRV_VALUE_SAMPLE`, one `hrv` field point per reading).
  **Buildable now**, directly from existing data.
- "Last 7 days" scatter/line (one point per day).
- A promotional banner at the bottom (cut off in the screenshot,
  likely more AI/Aura content) - probably skip, not explored further.

## Page: Resting Heart Rate (linked from Overview's Heart Health card)

Reuses the same layout as Heart Rate Variability almost exactly - same
`D / W / M / Y` selector, big-number + qualitative-label + "Slower -
Baseline - Higher" gauge (baseline shows `--`, same "needs more
history" state as HRV's baseline), "Last 7 days" chart. Differences:
label reads "Average" rather than "Good"/tier-name; ends with a static
educational blurb + "Learn more >" link (generic content, skip).

- **Buildable now**: yes, directly from existing `resting_heart_rate`
  data - nothing new needed, this page is almost a template match for
  the HRV page's layout.

## Page: Blood Oxygen (linked from Overview's Heart Health card)

- `D / W / M / Y` selector.
- Big number: a **range**, not a single value (e.g. "92%-99%"), with
  the selected time window shown as text underneath (e.g.
  "3 September, 16:00-17:00") - SpO2 is evidently treated as
  periodic/spot-check readings rather than one continuous stream, and
  the UI reflects that.
- Chart: **dashed/discrete bars**, not a continuous line - a
  meaningfully different visual treatment than the smooth HR line
  chart, appropriate for sparser data. Y-axis 80%-100%, same
  full-day-with-draggable-scrubber pattern as the HR chart, with the
  scrubbed-to window's individual readings highlighted (shown as short
  red tick marks at their exact %).
- Daily Summary card: Average / Maximum / Minimum % - same layout as
  the Heart Rate page's Daily Summary.
- "Add Data >" - manual SpO2 entry (not yet explored).
- "All Data >" - presumably a full log/list of individual readings
  (not yet explored).
- **Buildable now**: yes, directly from existing `spo2` data. The
  dashed/discrete chart treatment (vs. HR's continuous line) is worth
  replicating given how differently the two datasets actually behave
  (SpO2 readings are naturally sparse, forcing a continuous line
  through them would visually overstate how much data exists).

## Page: Heart Rate (linked from Overview's Heart Health card)

The Heart Health card actually links to **four separate pages**: Heart
Rate, Resting Heart Rate, Blood Oxygen, and Heart Rate Variability -
each its own button/destination. We don't have to mirror that exact
split, but worth knowing the real navigation structure.

- `D / W / M / Y` time-range selector (same reusable component as
  other pages).
- Big number (current/selected BPM) + timestamp.
- Full-day line chart (00:00-23:59, Y-axis 0-160) with a **draggable
  scrubber** at the bottom to move through the day, and a **fullscreen
  expand icon** - see "Zoomed chart pattern" below for what that opens.
- "Resting Heart Rate 57 BPM >" - links to its own page (not yet
  explored).
- Daily Summary card: Average / Maximum / Minimum BPM for the day -
  pure aggregation over the same data as the chart above.
- Heart rate zone card: a horizontal stacked bar + a 6-row legend
  (Relaxed/Light/Intensive/Aerobic/Anaerobic/VO2 max), each with time
  spent in that zone today. This 6-tier daily-summary system looks
  distinct from the 5-tier "Lactate Threshold" training zones on the
  dedicated config page (see below) - possibly two parallel zone
  systems.
- "Manually Data >" - links to manually-entered HR readings (matches
  `manual_heart_rate`, already extracted).
- Static "Heart rate" educational blurb at the bottom - generic
  content, skip.
- **Buildable now**: the main chart, daily summary, and resting-HR
  link - yes, from existing `heart_rate`/`resting_heart_rate` data.
  HR zones - yes as an approximation.

## Page: Heart Rate Zone configuration (settings, not a data display)

Linked from somewhere in HR-related settings (exact entry point not
noted). A real settings/config page, not a data view - relevant here
because it's what defines the zone boundaries used elsewhere:

- Header: back arrow, title, "Save" action (top right).
- "INTERVAL TYPE" section: 3 mutually-exclusive radio options (Max
  Heart Rate Zone / Reserved Heart Rate Zone / Lactate Threshold Heart
  Rate Zone), each with a short explanatory paragraph shown for
  whichever is selected.
- Below that, a section named after the selected interval type (e.g.
  "LACTATE THRESHOLD HEART RATE ZONE"):
  - "BASE VALUE" card: the numeric base (e.g. "Lactate Threshold Heart
    Rate: 164 >", tappable/editable) + an "Automatic Update" toggle
    (watch adjusts the base value over time from workout data).
  - "ZONES" list, with a "Reset" link: one row per zone, each showing
    a name, BPM range, and %-of-base-value range (e.g. "Active
    Recovery, 106-131 (65%-80%)"), each row tappable (presumably to
    manually override that specific zone's range).
- **Buildable now**: as a genuine settings feature (not mimicking
  Zepp's exact sync behavior) - yes, straightforward: store the
  person's chosen interval type + base value + zone %-bands as an app
  preference (wearable-events already has per-user config precedent),
  then compute zone-time from existing `heart_rate` data at query
  time - a real setting stored on our own side avoids
  needing that answered at all.

## Zoomed chart pattern (from the Heart Rate page's fullscreen button)

This is a genuinely well-designed mobile chart interaction, worth
building for real rather than just a plain line chart, if we want
dashboard charts to feel good to actually use one-handed:

- Rotates to landscape/fullscreen.
- A **thin overview strip** across the very top shows the *entire*
  day's data at low resolution, with a **highlighted rectangle**
  marking which portion of that day the main chart below is currently
  zoomed into - like a minimap.
- Below that: the selected point's exact timestamp + value, displayed
  as text (e.g. "03/09, 17:54:00" / "97 BPM").
- Main chart: a zoomed-in view (roughly a 1-hour window in the
  example) of the selected region, with its own Y-axis, a **draggable
  scrubber** at the bottom to move the zoomed window through time
  (updates the overview strip's highlighted rectangle to match), and
  a marker dot on the line at the currently-selected timestamp.
- A few icons on the right edge (possibly export/share/playback
  controls - not fully legible from the screenshot, revisit if this
  becomes worth building).
- **Buildable now**: yes, from existing continuous `heart_rate` data -
  this is purely a frontend interaction pattern, no new data needed.
  Likely a meaningful chunk of frontend work to get the synced
  overview-strip/zoomed-detail interaction feeling smooth, worth
  scoping as its own reusable component rather than building bespoke
  per metric.

### Sleep-context variant (from the Sleep Heart Rate page's fullscreen button)

A richer version of the same pattern, seen from the Sleep Heart Rate
page specifically - worth building as an extension of the base pattern
above rather than a separate component:

- **Multiple toggleable series** on one chart - seen here with Heart
  Rate (on) and Respiratory Rate (off) as checkable legend items,
  implying more than one metric can be overlaid/compared at once.
- **Sleep-stage lanes as the chart's background** - the hypnogram
  (Awake/REM/Light/Deep, our already-extracted stage data) forms
  horizontal bands that the HR/respiratory line is plotted against,
  not just a plain line chart - so a glance shows both "what my heart
  rate was doing" and "what stage I was in" at once.
- **Tap/select a stage segment** to see its own stats: shown as stage
  name + time range ("Awake 09:14-09:19"), duration ("6min"), and the
  selected series' value for that segment ("57BPM") - a nice compact
  way to inspect any specific stretch of the night without needing a
  separate table.
- **Buildable now**: yes, entirely from data already extracted (HR,
  respiratory rate, sleep stages all covered elsewhere in this doc) -
  purely a richer frontend treatment of the same base zoomed-chart
  component, not a new data requirement.

## Page: Sleep Heart Rate (linked from the Sleep tab)

- Avg BPM + "Slower - Faster" gauge (same baseline-gauge family as
  HRV/Resting HR).
- "Heart Rate" card: current BPM + timestamp, a full-night chart with
  the sleep-stage hypnogram as its background (see the zoomed-chart
  variant above - the same treatment appears here at normal size too,
  not just in fullscreen), draggable scrubber, fullscreen icon.
- "Last 7 days" trend (same family as other detail pages).
- "Health monitoring >" link - leads to the device's Health Monitor
  settings (not a data page - the real numbers found there are
  monitoring frequencies and alert thresholds).
- **Buildable now**: yes, entirely - this page
  is now confirmed (not just assumed) to be existing `heart_rate` data
  filtered to the sleep window, displayed against stage data we
  already have.

## Page: Sleep Respiratory Rate (linked from the Sleep tab)

Near-identical layout to Sleep Heart Rate - confirms the "physiological
signal during sleep, plotted against the stage hypnogram, with the
same fullscreen zoom" treatment is a general pattern, not something
built once for HR specifically:

- Avg BRPM + gauge, full-night chart with stage-hypnogram background,
  draggable scrubber + fullscreen icon (same zoom pattern - fullscreen
  view not captured this time, but confirmed to work the same way as
  the HR page's).
- "Last 7 days" trend.
- Educational blurb includes an actual reference range this time
  (unlike most, which are pure filler): "average sleep respiratory
  rate range under 66 years old is 12 to 20 breaths per [minute]" -
  informational only, not something we need as a threshold ourselves
  since no tier/gauge band uses it directly on this page.
- **Buildable now**: yes, entirely - matches already-extracted
  `sleep_respiratory_rate`.

## Page: Sleep Apnea Risk (linked from the Sleep tab / Time Asleep advanced page)

- `D / W / M / Y` selector (this page has all four, unlike the Time
  Asleep advanced page which only had W/M/Y).
- "Hypopnea (Suspected)" card: Avg times/hr + qualitative tier + a
  gauge with **four** confirmed fixed bands - same "thresholds shown directly on the gauge"
  pattern as Stress/Deep/REM/Awake, not user-configurable.
- "During Sleep" card: a genuinely different data shape than most
  sleep metrics - shows a **count of discrete suspected events** for
  the night plus their **specific time window(s)** (e.g. "1 time,
  06:00-07:00"), with a mini full-night timeline marking exactly where
  each event happened. Not just a rate - this
  changes what "extracting hypopnea data" would actually need to look
  like (individual timestamped events, not one summary number).
- "Last 7 days" trend.
- A promotional/decorative banner at the bottom (cut off, likely more
  AI/insight content) - skip.
- **Buildable now**: no - three
  independent reasons converge on "not feasible right now," not just
  "not yet investigated" - the mechanism is almost certainly SpO2-dip
  based (not respiration rate, confirmed against a real reported event
  showing no clear respiratory-rate change), Gadgetbridge itself has
  no decoded field for it and explicitly avoids computing this class
  of metric from raw wearable data (citing real published evidence of
  poor correlation with lab sleep studies), and our own extracted
  `spo2` data is event-triggered/sparse rather than continuous - likely
  too coarse for event-level detection even with the right algorithm.

## Page: Sleep Regularity (linked from Overview)

- A percentage-based quality scale (0-59% / 60-79% / 80-89% / 90-100%)
  - showed "Insufficient data" (needs more nights of history before
  Zepp itself will compute a value).
- "Last 7 days": avg fall-asleep time, avg wake-up time (as text), and
  a bar chart - axis is time-of-day (00:00 at top to 09:30 at bottom
  in this example), one bar per day, likely representing the sleep
  window (bedtime to wake time) rather than pure duration.
- "Went to bed" - a scatter chart, one point per day, Y-axis is
  bedtime (narrow range shown: 23:00-23:30).
- "Get up" - same pattern for wake-up time (cut off in the
  screenshot).
- **Buildable now**: yes, entirely computable from
  `sleep_session_start`/`sleep_session_wakeup` (already extracted)
  across multiple nights - no new raw data needed, this is purely an
  aggregation/analysis feature on top of what we already have. The
  "regularity %" scoring formula itself isn't known (Zepp's own
  algorithm, not something we can replicate exactly), but a simpler
  "bedtime/wake-time consistency" visualization (the scatter charts)
  doesn't need Zepp's specific formula to be useful.

## Page: Steps (linked from Overview)

- Header includes a small external-link/share icon (top right,
  purpose not explored - possibly share-to-social or open-elsewhere).
- `D / W / M / Y` selector.
- "Total" label + big number (steps) + date.
- Bar chart, 00:00-23:59, sparse discrete bars (steps are naturally
  bursty/event-driven) - same "dashed/discrete for sparse data" family
  as the Blood Oxygen chart, not a continuous line.
- Three-stat row: Distance, **Duration** (e.g. "00:21:00" - total
  active/walking time for the day, a field we hadn't seen before),
  Calories (with an info "?" icon, presumably explaining the
  calculation method).
- "Reached goal" card: total days and consecutive days hitting the
  daily step goal - gamification/streak tracking.
- "Weekly Steps" / "Monthly Steps" cards: a text summary sentence +
  a simple two-bar comparison (this period vs last, as steps/day) -
  a clean, reusable "this vs last" comparison visual worth considering
  for other metrics too (sleep, stress, etc.), not just steps.
- "Health guide" - static educational blurb, skip.
- **Buildable now**: the chart, three-stat row (steps/distance side),
  and weekly/monthly trend comparisons - yes, from existing `steps`
  data (Distance still flagged unconfirmed).
  "Duration" (active minutes) is new but likely derivable by counting
  minutes with steps>0 in the existing per-minute activity data.
  "Reached goal" streak tracking doesn't need
  anything from Zepp at all - same as the HR Zone thresholds, this can
  just be our own configurable goal + aggregation over data we already
  have, no sourcing question needed.

## Page: Stress

- `D / W / M / Y` selector.
- Big number + timestamp.
- Full-day bar chart (00:00-23:59, Y-axis 0-100), **bars colored by
  tier** (blue/green/orange/red-orange for
  relaxed/normal/medium/high), draggable scrubber - same family as the
  other full-day charts but with per-bar tier coloring, not a single
  color.
- Tier legend row (colored dots + labels: Relaxed/Normal/Medium/High).
- "Daily Stress" card: a pie chart of time-in-tier percentages +
  Max/Min/Avg row underneath - similar spirit to the Heart Rate page's
  zone breakdown, but a pie instead of a stacked bar.
- "Manual Data" list: specific manually-triggered readings, each row
  showing time + value, tappable (presumably for more detail per
  reading). **This is genuinely useful beyond just UI reference** - a real
  Zepp-reading list like this is how `stress_type_num`'s 0/1 meaning
  ended up confirmed (see `parser/amazfit/README.md`).
- Educational blurb at the bottom - unlike most others, this one is
  **not just generic filler**: it states the exact fixed stress-tier
  thresholds directly, so this specific blurb
  was worth reading in full rather than skipping.
- **Buildable now**: yes, entirely - full-day chart, tier coloring,
  pie breakdown, and Max/Min/Avg all come from existing `stress` data
  and the confirmed fixed thresholds. The "Manual Data" list is now
  also buildable cleanly: `stress_type_num` is CONFIRMED
  (see `parser/amazfit/README.md`) - 0=manual, 1=automatic - so manual-only readings
  are a straightforward tag filter, no remaining ambiguity.

### Weekly (W) zoom level - likely a shared pattern, not Stress-specific

Seen on the Stress page's `W` tab, but the `D/W/M/Y` selector is
reused across most pages, so this is probably the general shape every
metric's weekly view takes, not something unique to stress:

- Headline number becomes a **range** ("12-65") instead of a single
  value, with just the reference date underneath (not a day name).
- Chart becomes one **range-bar per day** (Mon-Sun) - a vertical bar
  spanning that day's min-to-max, not a filled/continuous chart like
  the daily view. Days with no data simply have no bar (matches the
  amount of real history so far - only Wed/Thu had bars).
- Simpler single-item legend (vs. the daily view's 4-tier legend).
- A 2x2 stats grid below: Avg, Max, Min, plus a metric-specific extra
  stat - for Stress this was "Single Stress Measurement: 3 time(s)"
  (a count of manual readings that week).
- **Buildable now**: yes, as an aggregation over whatever daily data
  we already have per metric - min/max/avg per day, rolled into a
  per-week range-bar chart. Worth building this as one shared
  component (reused by every `D/W/M/Y` metric page) rather than
  reimplementing per metric, given how similar it's likely to look
  everywhere.

## Page: Sleep tab (top-level tab, distinct from the Sleep Duration detail page)

This is the `SLEEP` tab from the top nav (Overview/HybridCharge/Sleep/
Exertion), not the "Sleep Duration" card's own detail page - a much
longer, richer page with its own set of sub-cards, several of which
link to their own further detail pages.

- **Sleep Score ring**: a big circular gauge, e.g. "78 FAIR" - a
  single composite score, separate from (and presumably built from) the
  individual sleep metrics below. Likely Zepp's own proprietary
  formula, not directly reproducible - we
  could compute our own version from data we already have, similar to
  the Sleep Regularity % situation.
- **Sleep Duration card**: duration + a real **hypnogram** - a
  horizontal timeline of colored blocks (by stage) across the night,
  with edit and fullscreen icons. **This is exactly what our own
  sleep-stage timeline extraction produces** - direct confirmation
  that decoding `HUAMI_SLEEP_SESSION_SAMPLE` was the right call, not
  just for the summary numbers but for this exact visualization.
- **Nap card**: separate from the main sleep session ("NO DATA" in
  this example, no nap taken that day) - **BUILT (2026-09)**: naps are
  fully decoded, extracted, and shown on the Sleep tab as their own
  section, matching Zepp's own real treatment as a distinct category -
  see `decode_sleep_session_blob()`'s own docstring for the full
  confirmed byte layout this is decoded from.
- **Sleep Metrics card**: five rows, each with a value + (where
  applicable) a percentage/count + a qualitative tier label (Good/
  Normal/"Pay Attention"):
  - Sleep Duration
  - Sleep Regularity
  - Deep Sleep (duration + % of total sleep)
  - REM Sleep (duration + % of total sleep)
  - Awake (duration + **count of wake events**, tier "Pay Attention" -
    a warning-level tier distinct from Good/Normal)
  - **Buildable now**: yes, entirely - all five rows come from data we
    already extract from the sleep session BLOB (total-per-stage
    minutes for duration/%, and counting individual awake-type stage
    segments for the wake-event count).
- **Sleep Tags section** (Bedtime Journal + Wake-up Mood): tappable
  pill buttons for pre-bed activities (Read/Music/"Work out late"/...)
  and post-wake mood (Excellent/Calm/Droopy/...). **This is user
  INPUT, not device data** - not a parsing question at all. Directly
  relevant though: wearable-events already has its own subjective
  sleep-journal feature (a 1-5 rating), and these tag-style options
  are worth revisiting as inspiration for extending that existing
  feature later - explicitly noted as a "like the 1-5 better than the
  mood [scale]" preference, so likely additive (bedtime-activity tags
  alongside the existing 1-5 rating) rather than a wholesale replacement.
- **"vs. Last 7 Days" weekly view**: a stacked bar per day (colored by
  stage, matching the hypnogram's palette) showing nightly sleep
  composition across the week - a nice compact way to show stage
  breakdown trends, buildable from the same underlying data as the
  daily hypnogram.
- **Hypopnea (times/hr)** trend card - **genuinely new**, not
  something we've seen before.
- **Respiratory Rate (BRPM)** trend card - matches already-extracted
  `sleep_respiratory_rate`.
- **Sleep Heart Rate (BPM)** trend card - a dedicated "heart rate
  during sleep only" view - likely just
  existing `heart_rate` data filtered to the sleep session's time
  window, not a separate data source, but not fully confirmed.
- **Weekly Trend Report / Monthly Trend Report** cards - link to
  further detail pages, not yet explored.

### Deep / REM / Awake drill-down pages (from the Sleep Metrics card)

All three share a layout, with one visual difference worth noting:

- Value (+ % for Deep/REM, + count for Awake) + qualitative tier label.
- Gauge bar with the **exact numeric tier boundaries printed on it**
  (e.g. "<10% / 10%-35% / >35%" for Deep/REM, "≤3 times / >3 times"
  for Awake) - this is
  the same "fixed thresholds, shown directly" pattern as the Stress
  page, not user-configurable like HR Zones.
- "Last 7 days" chart - **visual difference between the two page
  types**: Deep/REM show a *partial* bar (a gray "total sleep" bar
  with a colored portion showing this stage's share), since they're
  framed as a percentage of the whole; Awake shows a *fully-colored*
  bar, since it's framed as its own absolute value (minutes), not a
  share of something else. Worth replicating this distinction rather
  than using one bar style for all three.
- "Compared with other users" - not reproducible, same as elsewhere.
- Educational blurb - generic filler for Deep/REM ("What is deep
  sleep?", "What is REM sleep?"); Awake's version ("Why do I wake up
  during sleep?") is a partial exception, containing an actual
  corroborating fact ("normal to wake up 1 to 3 times") rather than
  being pure filler.
- **Buildable now**: yes, entirely - same data already covered by the
  Sleep Metrics card entry above.

## Page: Time Asleep (advanced) - linked from the Sleep tab's stacked-bar card

An important find: this is a **richer, more detailed page** than the
"Sleep Duration" page documented earlier (which is linked from the
Overview tab's Core Metrics list) - same underlying data, superset of
information. **Not clear why Zepp has two separate pages for
essentially the same thing** - worth treating this advanced version as
our real target/template rather than the simpler one, since it covers
everything the simple page does plus more.

- Only `W / M / Y` - **no daily (`D`) tab** on this particular page,
  unlike most other detail pages.
- Big number (total sleep time) + date.
- **Weekly stacked bar chart** (Mon-Sun): each day's bar is a stack of
  Deep/Light/REM/Awake segments (confirmed exact color order/mapping),
  **with Naps shown as a separate, distinctly-colored bar/legend
  entry, not folded into the main stack** - direct confirmation of how
  Zepp itself treats naps as a distinct category.
- "Average sleep score: 78 Points" - confirms Sleep Score is a real,
  named, averageable metric (not just a one-off daily gauge).
- A list of sub-metrics, each row showing an average value + a
  qualitative tier label + a link to its own further detail:
  Time Asleep, Deep, REM, Awake times, **Fell asleep** (average
  bedtime), **Woke up** (average wake time) - the last two are new
  named metrics here, though they're just `sleep_session_start`/
  `wakeup` we already have.
- **"Sleep Apnea Risk"** - a standalone linked card, no value shown
  directly on this page. Genuinely new.
- "Sleep status analysis" info card: explains some analysis features
  need "at least 2 times a week" of sleep tracking before they
  activate - a progressive-unlock UX note, not a data question.
- **"Night sleep regularity" chart** - a noticeably nicer
  visualization than the simple Sleep Regularity page's scatter-dot
  version: two overlaid smooth curves (Woke up / Fell asleep) across
  the week, Y-axis wrapping past midnight (labeled "09:19 (+1)" for
  times after midnight). Worth using this richer chart style as the
  target rather than the simpler scatter version, if only building one.
- Weekly **Bedtime Journal** / **Wake-up Mood** summary cards - same
  feature as before, shown here in a weekly rollup with an explicit
  "No record" empty state worth replicating for our own version.
- Generic "Sleep" educational blurb at the very bottom - skip.
- **Buildable now**: essentially everything except Sleep Score (needs
  our own formula) and Sleep Apnea Risk (needs the Hypopnea question
  resolved first) - all from data already extracted.

### M / Y zoom levels (Time Asleep advanced page specifically)

Confirmed these scale differently than the general `W` pattern
documented earlier - this page's `M`/`Y` tabs aren't just "the same
chart with a wider window", they change granularity:

- **`M` (Month)**: the stacked composition bar and the regularity
  chart both show **one entry per day of the month** (28-31 bars/points,
  x-axis labeled by date e.g. "01/09 ... 10/09 ... 20/09 ... 09/30") -
  same granularity concept as the weekly view, just more of it.
- **`Y` (Year)**: both charts collapse to **one entry per month** (12
  bars/points), each showing that month's *mean* composition/regularity
  rather than daily detail - a genuine rollup, not just more x-axis
  room.
- **Bedtime Journal / Wake-up Mood with real data** (previously only
  seen as empty "No record" states): each becomes a **ranked list of
  tag options**, one row per tag actually used in the period, showing
  a percentage of days + a day-count + a proportion bar (e.g. "Read
  50.0%, 1 Day"). "No record" appears as its own pseudo-tag row for
  days with nothing logged, not omitted. This is a clean, simple
  pattern worth matching for wearable-events' own bedtime-tag feature
  once built.
- **Buildable now**: yes - all of this is aggregation over data
  already covered elsewhere in this doc (sleep-stage composition,
  regularity timestamps) plus straightforward frequency-counting for
  the tag breakdown, once wearable-events has its own tag-logging
  feature to aggregate.

## Page: Activities list (linked from Overview's Activities card)

- Header: an "ALL" dropdown (filter by activity type, presumably),
  add (+) and export icons.
- Date range shown as text + summary totals for that range: Total
  mileage, Total times, Total duration, Total Calories.
- A day-of-week strip (calendar-style week view).
- A day-grouped list below: date headers, then one row per activity
  that day (icon, name, start time, duration/calories/avg-HR mini
  stats).
- **Period selector at the bottom is a different pattern** than the
  `D/W/M/Y` used elsewhere: rolling windows - `7` (days) / `30` (days)
  / `365` (year) / "All" - not calendar-aligned week/month/year.
- **Buildable now**: only the parts backed by data we already have
  (this list could show entries from our own already-extracted
  activity/HR data at a coarse level) - the rich stats shown per entry
  mostly come from the workout-summary source (see
  `parser/amazfit/README.md` for the confirmed field list).

## Page: Individual Activity/Workout detail (e.g. "Hybrid training")

**Deliberately not a near-term build target** - flagged by the person
as a later project. The underlying workout-summary data source (Zepp
OS devices' `RAW_SUMMARY_DATA` protobuf blob) IS now parsed and
extracted (see parser/amazfit's own README.md) -
what follows was originally written before that, from Zepp app
screenshots alone, and has now been cross-checked against BOTH a
second set of real screenshots of this exact same workout (2026-09,
Gadgetbridge's own app this time, not just Zepp) AND the actual
extracted InfluxDB fields for that same row - genuinely confirmed, not
still-speculative. **Confirmed for "Hybrid training" specifically -
the person has since logged other (particularly outdoor) workout types
and observed the screens differ, not yet documented here.**

### Zepp app (the vendor app, not Gadgetbridge)

- Dark header banner: user avatar/name, timestamp, device chip
  (confirms which physical device logged it - relevant once
  multi-device workout data exists), big Calories number.
- Stats row: Workout time ("24:00" with a small superscript "68" -
  meaning unconfirmed, possibly sub-second precision - not
  investigated further), Avg heart rate, **Training Load** (a single
  number) - all three map directly onto extracted fields:
  `active_seconds`, `hr_avg`, `training_load`.
- "Detailed data >" link (still not explored).
- HR chart, but **workout-relative time** (00:00 to the workout's own
  duration, not a day-of-day axis like other charts) - with a second
  tab for "Post-Workout HR" (truncated in the screenshot to "POST-
  WORKOUT HE..." - a distinct recovery-period series, not explored
  further) and the usual zoom icon.
- "HEART RATE ZONE" card: **5 zones shown, high-to-low BPM**:
  Anaerobic endurance (162-178bpm), Lactate threshold (152-161bpm),
  Aerobic endurance (144-151bpm), Efficient Fat Burning (132-143bpm),
  Active Recovery (106-131bpm) - each with a %/time-in-zone specific
  to the session. **Zepp's own zone NAMES do NOT match Gadgetbridge's
  internal zone names or this app's own extracted field names** - see
  the dedicated callout below, this is the single most important thing
  to know before building a UI against `hr_zone_*`.
- "TRAINING EFFECT" card: two gauges (Aerobic, Anaerobic) - maps onto
  `aerobic_training_effect`/`anaerobic_training_effect`. Each also
  shows a qualitative label ("Good" for 3.2 aerobic, "No effect" for
  0.9 anaerobic). **RESOLVED (2026-09)**: this is Firstbeat's own
  publicly-documented, industry-standard 0-5 Training Effect scale
  (firstbeat.com/en/science-and-physiology/epoc-and-training-effect) -
  widely licensed across wearable brands, not a Huami/Zepp secret:

  | Range | Firstbeat's own label |
  |---|---|
  | 0.0-0.9 | No effect |
  | 1.0-1.9 | Minor effect (recovery training) |
  | 2.0-2.9 | Maintaining effect |
  | 3.0-3.9 | Improving effect |
  | 4.0-4.9 | Highly improving effect |
  | 5.0 | Temporarily overreaching effect |

  Confirmed consistent with BOTH real data points already extracted:
  0.9 and 0.6 (Hybrid Training's anaerobic, Walking's aerobic) both
  land in "0.0-0.9 - No effect", matching the real screenshots exactly.
  3.2 (Hybrid Training's aerobic) lands in "3.0-3.9 - Improving effect"
  - Zepp's own shown label was simply "Good", presumably its own
  simplified wording over the same underlying scale rather than a
  different scale entirely - the numeric boundaries are what matter for
  reproducing the label, and those match. Amazfit's own technology page
  independently confirms the same 0-5 range and "5.0 - high body load,
  needs recovery" framing (us.amazfit.com's own "Running Technology"
  page). Deliberately NOT computed as a stored field in the parser
  itself (this app's own established pattern - see e.g. SRI's "no
  invented tier labels" - a label is a presentation concern) - a future
  frontend can apply this lookup directly against the already-extracted
  raw `aerobic_training_effect`/`anaerobic_training_effect` values.
- "LAP DETAILS" table: No./Time/Heart rate/Calories burned, one row
  per lap - laps appear to be auto-segmented (this example's laps are
  ~24min and ~5sec, suggesting an automatic "final short segment"
  rather than user-triggered lap presses, though not confirmed). NOT
  in the `RAW_SUMMARY_DATA` protobuf schema at all (`WorkoutSummary`
  has no repeated-lap field) - confirmed via Gadgetbridge's own real
  Java source (`ZeppOsActivitySummaryParser.enrichWithDetails()`) that
  laps live in the separate per-workout details export instead.
  **RESOLVED (2026-09)**: that separate export is now extracted -
  Gadgetbridge's own "Auto export FIT tracks" automation produces a
  real per-workout `.fit` file (confirmed reachable via the same
  WebDAV sync as `Gadgetbridge.db`, correlated to its own
  `BASE_ACTIVITY_SUMMARY` row by a shared filename timestamp), and FIT
  files have a genuine "lap" message type - now decoded via
  `flatten_fit_laps()` into `sample_type: "workout_lap"` points
  (avg/max HR, avg/max cadence, distance, calories, duration,
  avg/max speed, ascent/descent, 1-indexed `lap_number`). GPX-sourced
  workouts (no FIT export available) correctly produce zero laps - no
  standard lap concept in GPX.
- "YOUR RPE" card: a self-reported Rate of Perceived Exertion (e.g.
  "7 VERY HARD"), editable - **user input, not device data**.
- "YOUR WORKOUT BALANCE" card: opens a modal with an Endurance-vs-
  Strength percentage slider (50/50 in the example) + Save/Cancel.
  **Not user input** - most likely **computed** from the workout type
  and possibly per-workout HR zone time distribution, with the slider
  allowing the person to override the computed default rather than
  enter it from scratch (same "computed but adjustable" pattern as the
  Lactate Threshold HR base value's "Automatic Update" toggle).
  **Investigated (2026-09), genuinely still unresolved**: several
  targeted searches for Amazfit/Zepp's own public documentation of a
  per-workout endurance/strength balance formula came back empty -
  found only a DIFFERENT, newer, WEEKLY-level "Training Balance"
  feature (part of 2026's Balance 3/Balance Ultra "Hybrid Training
  System" launch), not the per-workout metric shown in this screenshot.
  Unlike Training Effect (below), no public formula surfaced anywhere
  - treated as genuinely proprietary/undocumented for now, not just
  unattempted, similar in category to the earlier Sleep Apnea Risk
  conclusion. Would need either an official source turning up later,
  or enough real (workout type, HR zone distribution, shown Balance
  value) data points to attempt a statistical fit - not attempted
  here, real data for that doesn't exist yet.
- "WORKOUT NOTES" - "Add how it felt", freeform - **user input**, same
  spirit as the Sleep tab's own subjective journal.
- "Any data issues? Tap to give feedback" - app feedback link, skip.

### Gadgetbridge app (a completely different, separate UI from Zepp's)

New this session - screenshots of the SAME real workout in
Gadgetbridge's own app, confirming several things Zepp's screens alone
couldn't:

- Title bar reads **"Unknown activity"** (Gadgetbridge's own honest
  fallback - it doesn't recognize this workout's activity type code),
  with **"Hybrid training" shown separately as a sub-heading below
  it**. This directly CONFIRMS (not just speculates) that the extracted `name`
  tag is a person-assigned label, not anything Gadgetbridge itself
  decoded from the activity type - it visibly doesn't know what this
  workout even is, while still showing the name the person gave it.
- Stats grid: Active/Heartrate/Active calories/Workout Load, then a
  second Heart Rate section with Heartrate/Max Heartrate/Min Heartrate
  - confirms `active_seconds`/`hr_avg`/`calories_kcal`/`training_load`/
  `hr_max`/`hr_min` are exactly the right fields, no others needed for
  a basic summary card.
- HR chart: same shape as Zepp's own (workout-relative time), not
  duplicated detail here.
- "Heart Rate Zones": **N/A / Warm-Up / Fat Burn / Aerobic / Anaerobic
  / Extreme** - this is Gadgetbridge's OWN naming, and it matches this
  app's own extracted `hr_zone_*` field names exactly (both are
  mirrored from the same Java source). Durations cross-checked
  directly against the extracted InfluxDB values for this exact row -
  every single one matches:

  | Extracted field | Value | Gadgetbridge's own label | Zepp's own label (same duration, different name) |
  |---|---|---|---|
  | `hr_zone_na_seconds` | 87s | N/A | *(not shown in Zepp's list at all)* |
  | `hr_zone_warm_up_seconds` | 539s | Warm-Up | Active Recovery |
  | `hr_zone_fat_burn_seconds` | 348s | Fat Burn | Efficient Fat Burning |
  | `hr_zone_aerobic_seconds` | 372s | Aerobic | Aerobic endurance |
  | `hr_zone_anaerobic_seconds` | 76s | Anaerobic | Lactate threshold |
  | `hr_zone_extreme_seconds` | 0s | Extreme | Anaerobic endurance |

  **The two apps use completely different names for the same
  underlying zones** (confirmed by matching each zone's duration AND
  its position in the BPM range, low-to-high) - Gadgetbridge's own
  code (and this app's extracted field names, which mirror it exactly)
  calls the second-highest-BPM zone "Anaerobic", while Zepp's own UI
  calls that exact same time range "Lactate threshold" and reserves
  "Anaerobic endurance" for the TOP zone instead (which Gadgetbridge
  calls "Extreme"). A future frontend showing these to the person
  needs to pick ONE naming convention deliberately - showing
  Gadgetbridge's own names next to Zepp's own BPM ranges without
  reconciling them first would read as simply wrong to someone used to
  the Zepp app.
- "Training Effect": same 3.2/0.9 numbers as Zepp, but WITHOUT the
  qualitative "Good"/"No effect" labels Zepp adds - confirms those
  labels are a Zepp-app-only presentation layer, not anything stored
  in the underlying data itself.
- "GPS track" section: "No maps found - please select a valid maps
  folder in the settings", with just a distance-scale legend and no
  actual map. This workout's own `SUMMARY_DATA` JSON has
  `"internal_hasGps":"true"` (which is why `flatten_workout_summary()`
  finding a `location` field at all would set `has_gps`-equivalent
  state, matching `ZeppOsActivitySummaryParser.java`'s own
  `summaryData.setHasGps(true)` call) - so Gadgetbridge itself believes
  this workout has GPS data, but has no local map tiles configured to
  actually render it. Worth remembering this "No maps found" message
  is a LOCAL rendering gap in Gadgetbridge's own app, not evidence the
  underlying GPS data is missing - don't take it as confirmation
  `hasGps` is wrong just because no map showed up here.

### Outdoor workouts (e.g. "Walking") - real screenshots, 2026-09

A second real workout (an outdoor walk, 0.39mi/6:49), confirming the
above extends to GPS-tracked activities with some genuinely new
sections, and a few findings worth flagging. Zepp screenshots only
this round; person's own direct comparison against Gadgetbridge's
equivalent screens (not separately screenshotted) noted inline.

- **GPS map**: Zepp shows the route as an orange line over its own
  street-map tiles, plus live weather at the time (temp/humidity/wind)
  pulled from an external source, not device data - skip. Person's own
  comparison: **Gadgetbridge shows the same track with no basemap at
  all** (just the line on a blank background) - consistent with the
  "No maps found" gap already seen on the Hybrid Training workout,
  except THIS workout genuinely has real GPS to show, so Gadgetbridge
  can render the track itself, just without street context tiles.
- **Elevation (ft) card**: Avg/Min/Max/Elev gain, plus a line chart -
  maps onto `altitude_avg_m`/`altitude_min_m`/`altitude_max_m`/
  `elevation_gain_m`. **The "(ft)" unit label is Zepp's own locale-
  based display conversion, not evidence the extracted fields are
  wrong** - Gadgetbridge's own real Java source (seen directly this
  session) explicitly tags these fields `UNIT_METERS` before Zepp ever
  sees them; a US-locale Zepp install converting to feet for display
  is a separate, later step. Same reasoning applies to the "STRIDE
  (in)" card further down - the `.proto`'s own field comment says
  "cm", Gadgetbridge's Java code never contradicts that, so `in` here
  is Zepp's own display conversion too, not the real stored unit.
  **Elevation LOSS wasn't shown as its own stat here** (only "Elev
  gain") despite the chart's own up-down-up-down shape implying real
  descent happened - `elevation_loss_m` is presumably still being
  written correctly (Gadgetbridge's own code always reads both), just
  not always surfaced as a headline number in Zepp's own layout.
- **Person's own direct observation, worth recording as a real data-
  quality flag**: Gadgetbridge's own elevation TRACK rendering for
  this exact workout showed an implausible instantaneous jump (~90ft
  down to ~60ft in what reads as a single sample), which Zepp's own
  chart for the same workout does NOT show (a smooth, symmetric
  up-down-up-down curve, matching the low-resolution avg/min/max
  summary numbers this app already extracts). This app currently only
  extracts the SUMMARY-level altitude numbers (avg/min/max/gain/loss)
  from `RAW_SUMMARY_DATA` - a full per-sample elevation TRACK isn't
  part of the `WorkoutSummary` protobuf schema at all (same situation
  as Lap Details - it would live in the separate `RAW_DETAILS_PATH`
  source, not yet explored). Worth remembering if that per-sample
  track ever becomes an extraction target: Gadgetbridge's own decoding
  of it may have real quality issues independent of anything in this
  app's own code, going by this direct side-by-side comparison.
- **GRADIENT DISTRIBUTION card** (Zepp only - person confirms
  Gadgetbridge doesn't show this one): a donut chart, Uphill/Flat/
  Downhill as both a duration and a %. Cross-checked directly against
  this app's own already-extracted fields: Uphill (33s) + Flat (310s)
  + Downhill (66s) = 409s, matching the workout's own total duration
  exactly - Uphill/Downhill here are simply `ascent_seconds`/
  `descent_seconds`, and "Flat" is just the remainder (total duration
  minus both). No new field needed to reproduce this card - it's fully
  computable from what's already extracted.
- **CADENCE (spm) card**: Avg/Max plus a scatter chart - same
  `avg_cadence_per_min`/`max_cadence_per_min` fields already extracted
  for Hybrid Training (steps.avgCadence/maxCadence, the *60 scaling
  already confirmed from Gadgetbridge's own Java source).
- **STRIDE (in) card**: Avg/Max plus a chart, color-coded (red/orange/
  green/teal bands visible, meaning unconfirmed - possibly a pace-
  quality indicator, not investigated). Avg maps onto `avg_stride_cm`
  (see the unit-conversion note above) - **Max stride isn't in the
  `.proto` schema at all** (`Steps` only has `avgStride`, no
  `maxStride` field), so Zepp's own "39 Max" here must be computed
  client-side from a per-sample stride stream this app doesn't have
  access to (same category as the elevation/GPS track - lives outside
  `RAW_SUMMARY_DATA`, if it exists as stored data at all rather than a
  live-only value).
- **SPEED (mph) card**: Avg/Max plus a chart. **RESOLVED (2026-09)**:
  `avg_speed_mps`/`max_speed_mps` now extracted and confirmed exactly
  against this exact real walk's own values - decoded the real
  `RAW_SUMMARY_DATA` blob, cross-checked `pace_avg_raw*1000` against
  Gadgetbridge's own `SUMMARY_DATA` JSON (`averageKMPaceSeconds:
  648.9649` - exact match), and confirmed the derived m/s values
  convert to exactly 3.45/3.94 mph, matching Zepp's own displayed
  avg/max speed for this precise workout. The swim-vs-non-swim scaling
  split (Gadgetbridge's own code uses a different formula for swims)
  is resolved via `summary.HasField("swimmingData")` as a direct,
  reliable proxy - no need for the still-unconfirmed activity-type-code
  mapping for this specific purpose. Raw `pace_avg_raw`/`pace_best_raw`
  kept alongside for transparency.
- **HEART RATE ZONE**: same 5-zone system as Hybrid Training, same
  Zepp-vs-Gadgetbridge naming mismatch already documented above -
  for this low-intensity walk, effectively all time (98%) fell in the
  lowest zone (Active Recovery/Warm-Up), a useful confirmation the
  zone breakdown genuinely varies by workout rather than being a fixed
  per-activity-type default.
- **TRAINING EFFECT**: only the Aerobic gauge shown here (0.6, "No
  effect") - **no Anaerobic gauge at all**, unlike Hybrid Training
  which showed both. Most likely Zepp conditionally hides a gauge
  when its own value is negligible for a low-intensity activity like
  walking, rather than this app's own extraction missing anything -
  `anaerobic_training_effect` is presumably still written as a real
  (near-zero) number even when Zepp's own UI doesn't bother showing
  it. Slightly narrows the still-unconfirmed qualitative-label
  thresholds: 0.6 and 0.9 both read "No effect", 3.2 reads "Good" -
  the real cutoff sits somewhere in between, still not pinned down.
- **LAP DETAILS**: **different columns than Hybrid Training** -
  Distance/Speed/Time here, vs. Time/Heart rate/Burned there. Columns
  are activity-type-dependent (distance-based for outdoor, HR/calorie-
  based for indoor) - same auto-segmented pattern either way (one real
  lap plus a tiny few-second trailing "cleanup" lap). **RESOLVED
  (2026-09)**: now extracted from FIT's own real "lap" message type
  regardless of activity type - `flatten_fit_laps()` extracts whatever
  subset of fields a given lap actually carries (distance/speed for
  outdoor, HR/calories always present either way), matching the same
  "only write what's present" approach as the per-sample data.
- **YOUR WORKOUT BALANCE**: 85/15 here vs. 50/50 for Hybrid Training -
  confirms this genuinely varies per workout (a walk reading heavily
  endurance-leaning makes sense), reinforcing rather than replacing
  the existing "computed, not a fixed default" hypothesis. **Still
  unconfirmed after real research effort (2026-09)** - see the fuller
  note on this under the Hybrid Training section above; no public
  formula found for the per-workout version specifically.
- **"GEAR DETAILS" section, RESOLVED (2026-09)**: this is Zepp's own
  "Gear Management" feature (confirmed via the Zepp app's own App
  Store listing: "Easily add your gear to automatically track its
  usage mileage") - a person manually assigns gear (shoes, bikes, etc)
  to workouts within the Zepp app itself, for mileage tracking, same
  category as Strava's own gear feature. This is Zepp-app-side,
  person-entered metadata, not anything the watch itself measures -
  almost certainly never reaches Gadgetbridge's own synced data at
  all, same category as RPE/Workout Notes (user input, not device
  data) rather than a missed extraction target.

### Buildable now vs. still blocked

The genuine user-input pieces (RPE, Workout Notes) don't need any
device data at all if we want our own version - same category as the
existing Sleep tab's subjective journal. Gear Details is resolved as
out of scope entirely (person-entered Zepp-app metadata, not device
data). Everything else on this page now HAS its underlying data
extracted - HR/HR zones incl. real BPM thresholds, training effect
(incl. the qualitative label thresholds), load, calories, altitude/
elevation, cadence, speed, laps, and per-sample GPS/HR/cadence/
elevation/speed data (see parser/amazfit's own README.md for the
full field list and the FIT/GPX/DB-only priority
chain). The ONE remaining genuinely unresolved piece is **Workout
Balance** - a real research effort found no public formula anywhere,
only a different, newer weekly-level feature - treated as a real dead
end for now, not something to keep chasing without new information.
