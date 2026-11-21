I read all of `src/Shell.tsx` and how it is mounted (`src/analytics/Widget.tsx`), plus the parts of `src/ScrollBar.tsx`, `src/Router.tsx` and `styles/app.css` cited below. I couldn't run git here, so the uncommitted diffs to `src/Tuner.tsx` and `styles/app.css` are not checked.

**Short answer:** the overall design holds up, but the "stay put while the user is active" rule is broken in two places. The cursor-to-Help landing can fire while someone is scrolling and then ignores their scrolls. A page change during a landing usually doesn't call the widget back up.

## What holds up

- **It survives page changes.** It's mounted once in the root layout via `app/layout.tsx:156` `<HelpWidget />` → `src/analytics/Widget.tsx:102` `{ready && <Shell open={open} />}`. So a client-side page change keeps the same widget, as the comment at `src/Shell.tsx:72` intends.
- **Page changes count as activity while it's moving.** `src/Shell.tsx:1006` `navT = t; engT = t; maxScroll = scrollY; ...` feeds the idle rule at `:752` `if (tune.idle > 0 && t - eng > tune.idle && t - stayFrom > tune.stay)`.
- **Page scroll resets don't fake a "back at top" or "bottom" landing.** Both need the user's own input after the last page change (`:753` `userT > navT`, `:755` `lastScroll > navT && userT > navT`). Jumps of half a screen or more are treated as page resets, not the user (`:926`).
- **The physics is frame-rate independent** (fixed step at `:23` `const STEP = 1 / 240;`), and a hidden tab snaps it home (`:1018-1031`).

## Failure modes (worst first)

**1. The cursor-to-Help trigger fires while the user is scrolling, then locks them out (high).**
- The button is fixed in the bottom-right corner (`styles/app.css:7-8`). The scrollbar is on the right edge, so moving the pointer toward the lower part of the scrollbar comes within 150px of the button.
- The trigger at `:1042` `if (phase === 'chase' && tune.dock > 0 && !holdBusy && pd < tune.dock && pd < prevBtnD - 0.5 && !scrollBar.dragging)` only skips an active drag. It doesn't skip the native scrollbar at all: `pointerHeld` (`:992`) isn't checked.
- Once `docking` is set, every scroll during the landing is ignored: `:940` `if (docking || (mouse && btnDist(mouse.x, mouse.y) < tune.dock && !scrollBar.dragging)) return;`. The user drags the bar and the widget keeps landing.
- **Fix:** don't trigger when `pointerHeld`, when the pointer is in the scrollbar column (`mouse.x >= gx() - tune.size`), or within ~300ms of the last scroll.

**2. A page change during a landing doesn't count as activity (medium).**
- `:1008` `if (phase === 'chase') start();`: during `'land'`, `navigated` only updates timestamps.
- The widget only relights if the page change happens to cause a scroll event (`:939-951`).
- **Fix:** move the relight branch out of `onScroll` into a function and call it from `navigated` when `phase === 'land' && !docking`.

**3. "Back at top" can use a stale scroll depth from the previous page (low; depends on timing I haven't verified).**
- `navigated` sets `maxScroll = scrollY` (`:1006`) inside the click capture handler, which may run before the router scrolls to 0 (`src/Router.tsx:551` `scrollTo({ top: 0, behavior: 'instant' });`).
- If the old depth was between 300px and the jump threshold, `:926` won't reset it.
- **Fix:** in `navigated`, use `maxScroll = 0`.

## Assumptions / what would change this

- I assumed the router resets the scroll after the click capture handler runs. If it runs first, #3 goes away.

Verdict: REVISE
Confidence: medium
