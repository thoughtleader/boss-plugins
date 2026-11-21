## Issue 1: Due-date reminders

### Real and helpful?
**Yes.** The gap is real: the app has a due-date field (`tasks.due_at`), a sort by due date, and a "today" filter, but nothing tells the user when a task becomes due. Opening the app is the only way to find out. The "everything fires at once after a long weekend" problem is a known failure in small task apps.

### Right approach given precedent?
**Mostly yes, with one calibration gap.**
- ✅ Extending the `tasks` row (no new table) aligns with precedent: "optional fields live on the row, not beside it".
- ✅ Default lead time from the settings store, else one built-in value, matches how the sort option is handled.
- ✅ A short section in the README plus one test file mirrors the pattern used for the earlier archive feature.
- ⚠️ **The ten-minute default** appears chosen from one person's habits. Either point to the usage data that produced it, or write "about ten minutes (configurable)" and drop the false precision.

### Insight or generic?
**Above average.** Three distinctions packed into a few lines set this apart from a plain wish:
- A reminder belongs to the task, so completing the task cancels it.
- Snooze edits the time; it never adds a second reminder.
- Missed reminders collapse into one summary on the next launch.

### Sharpening
- Lead with the third distinction; it is the least obvious.
- Cut the comparison to other apps, which a maintainer already knows.
- Add the migration as a single statement.
