# Council review: issue 1 (due-date reminders) and issue 2 (drag to reorder)

## Issue 1: Reminders fire once, at the chosen time

**Is it real and helpful?** Yes. Two failures are common in small task apps:

- A task due today is buried under older tasks and nobody notices it.
- A reminder that repeats on every launch gets muted within a week.

The precedent file shows the app has no notion of "notify" at all, so users have nothing to build on.

**Is the fix the right approach?** Mostly yes. It follows precedent closely:

- A nullable `remind_at` column on the task, with no new table.
- The default lead time comes from the settings store, with one built-in default of ten minutes.
- No new permission prompt until the first reminder is set.
- About thirty lines of change, one test file, and a short note in the README.

The structure is right.

**How much insight does it show?** Mid-level, not exceptional. "Remind me once" is what every calendar already does. The parts a careful product person would call sharp are:

- "A reminder belongs to the task, not to the list."
- "Completing a task cancels its reminder without asking."
- "Snooze moves the time; it never creates a second reminder."

**Problems and ways to sharpen it:**

- Say what happens when the device clock changes time zone.
- Name the smallest lead time you will accept, and why.
- Drop the second code sample; it repeats the first.
