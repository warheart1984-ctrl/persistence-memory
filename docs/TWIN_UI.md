# AI Twin UI

The dashboard is a read-only client for the tenant-scoped Twin API. The
existing FastAPI asset allowlist serves `ui/twin/index.html`, `app.js`, and
`styles.css` at `/ui/twin`; the route is dark unless `JARVIS_TWIN_ENABLED` is
on. The narrator endpoint also requires `JARVIS_TWIN_NARRATOR_ENABLED`.

## Screen

- Dark, responsive two-column page titled **Twin**. The header shows the
  tenant identity and `as_of` formatted in America/New_York, the configured
  provider picker, refresh control, and a text-labeled status pill.
- Facts column shows the coverage index with the truth disclaimer, all eight
  components and the weakest component, active projects, recent verified
  accomplishments, open risks, stale tasks, and the recommended mission.
- Narration column shows Assessment, Opportunity, Risk, Next action, and
  Explanation. Each gated sentence includes citation buttons; activating one
  scrolls to and highlights the cited fact. Template sentences carry a
  template label.
- The drop panel shows only section and reason. It never renders dropped
  model text. The expandable receipt includes state/input/prompt/raw/final
  digests, provider/model, latency, and fallback state. The receipt's
  `prompt_digest` is shown as **Prompt** because the API intentionally returns
  a digest rather than prompt content. Copy JSON copies only the receipt.
- Loading, disabled (404), API error, and empty-ledger states are represented.
  The empty-ledger message is “No evidence yet. Write your first evidenced
  memory.”

## Boundaries and accessibility

The browser calls only `/api/jarvis/twin/state`,
`/api/jarvis/twin/providers`, `/api/jarvis/twin/narration`, and the existing
read-only memory-record route when a record ID is opened. It contains no
provider credentials or model calls and has no write controls. Narration is
rendered only from the gated API response; all untrusted strings enter the DOM
through `textContent`. The UI uses semantic headings and landmarks, keyboard
operable controls, visible focus, reduced-motion support, accessible meter
values, and labels in addition to color.

## Local review

Enable the twin and narrator flags in a local or throwaway environment, start
the app using the repository's normal development command, and open
`/ui/twin`. Keep both flags off on a server where the feature should remain
dark. Verify keyboard navigation, a configured `none` provider, the 404 state,
an empty ledger, and a fake-model response that produces a gate drop. Confirm
the dropped sentence text is absent while the reason code appears.
