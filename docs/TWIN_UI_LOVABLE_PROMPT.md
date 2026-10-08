# Lovable prompt: AI Twin dashboard

Build a responsive, dark-by-default, read-only page titled **Twin** for the
Persistence Memory API. Treat every API response as untrusted data. Do not
add database access, browser-side model calls, write actions, API-key inputs,
or any persistence.

## API contract

- `GET /api/jarvis/twin/providers` returns `{ "providers": [{"name": str,
  "adapter": str, "model": str}] }`. Populate the provider picker from this
  list; select `none` by default. The browser must not submit a URL or key.
- `GET /api/jarvis/twin/state` returns `{ "state": TwinState.v1 }`.
- `GET /api/jarvis/twin/narration?provider=<configured-name>` returns
  `{ "state": TwinState.v1, "narration": {assessment, opportunity, risk,
  next_action, explanation}, "receipt": TwinNarrationReceipt.v1 }`. Render
  only the narration sentences returned here: they already passed the server
  gate. Use this response's state snapshot for citation highlighting.
- A 404 from the Twin routes means **Twin is turned off on this server**.
  Do not retry against a guessed address. Show a useful loading and error
  state for other failures.

## Layout

Create one laptop- and phone-friendly page with:

1. A header with “Twin”, `state.identity`, `state.as_of` formatted for
   America/New_York, provider picker, refresh button, and a text status pill:
   `Gated`, `Gated, N dropped`, or `Fallback`.
2. A facts column with coverage index and its disclaimer, components V/P/L/W/
   S/T/C/N as labeled horizontal meters, weakest component, active project
   chips, accomplishments, open risks, stale commitments with idle days, and
   recommended mission. Record IDs should open the existing read-only record
   endpoint `/api/jarvis/memory/{id}` in a separate tab.
3. A narration column with Assessment, Opportunity, Risk, Next action, and
   Explanation cards. Show each sentence's citation paths as keyboard
   operable chips; focus or activation scrolls to and highlights the cited
   fact. Mark template sentences with a subtle “template” badge.
4. A collapsed “Dropped by gate” disclosure showing only section and reason
   code, never dropped sentence text.
5. A collapsed receipt disclosure with state digest, twin input digest,
   provider/model, prompt digest (the API intentionally does not return prompt
   contents), raw/final output digests, latency, fallback state, and a copy
   receipt JSON button.

For `record_count === 0`, display “No evidence yet. Write your first
evidenced memory.” Keep the mission visible. For a model fallback, retain the
deterministic narration and show the fallback status and reason code from the
receipt when available.

## Safety and accessibility

- Keep all UI read-only. No buttons may create, update, delete, or persist
  records.
- Never display unchecked model output. Dropped text must not enter the page,
  logs, accessibility tree, or copied receipt.
- Never collect, store, or send provider credentials. Never call a model from
  the browser.
- Use semantic headings/landmarks, keyboard navigation, visible focus,
  reduced-motion support, WCAG AA contrast, accessible meter labels and
  values, and text in addition to color for every status.
- Use safe text rendering; do not inject API strings as HTML.
- Keep the feature dark when its server flags are off. Do not deploy it.
