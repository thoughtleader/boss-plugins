## Issue 1: Due-date reminders

### Real and helpful?
**Yes.** The app already stores a due date (`due_at`), a "today" filter and a sort by date, yet nothing tells the user a task has become due. The only way to find out is to open the app, which defeats the purpose of a due date. Reminders that collapse into one summary after a long absence is a pattern that works well in small task apps.

### Right approach given precedent?
**Mostly yes.**
- ✅ Adding a nullable `remind_at` column to `tasks` matches precedent: optional fields live on the row.
- ✅ Taking the default lead time from the settings store follows the sort-order option.
- ✅ A short README section and one test file mirror the earlier archive feature.
- ⚠️ The built-in ten-minute default has no stated source. Either cite where it came from or say "about ten minutes (configurable)".

### Top-tier insight or generic?
**Better than generic.** The useful ideas are few and well chosen:
- Completing a task cancels its reminder.
- Snooze moves the time instead of adding a second reminder.
- Reminders missed while the app was closed fire once, as one summary.

### Problems and ways to sharpen it
- Say what happens when the device changes time zone.
- Put the migration in one statement.
- Drop the second code sample, which repeats the first.

### Issue 2: Drag to reorder
Smaller in value than issue 1. Say whether it targets a short daily list or a long backlog, and use a sparse `position` value so a drop touches one row.
