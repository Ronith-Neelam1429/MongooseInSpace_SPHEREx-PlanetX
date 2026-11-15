// Pure state updates of the Candidates panel list, kept apart from the component for tests.

/**
 * The list after a "Show more" answer `r` (null: the route is gone) to a request made for
 * `requested`. An answer for another filter than the one now shown (the filter changed while it
 * was loading) is dropped, so one filter's page never lands in another's list. Rows already shown
 * are skipped: offset paging shifts when the survey stores new rows in between (newest first puts
 * them at the top), which would otherwise repeat the rows at the page boundary.
 */
export function mergePage(l, r, requested) {
  if (l.status !== 'ok' || l.filter !== requested) return l;
  if (!r) return { ...l, loadingMore: false };
  const shown = new Set(l.candidates.map((c) => c.id));
  const fresh = r.candidates.filter((c) => !shown.has(c.id));
  return { status: 'ok', filter: requested, total: r.total, candidates: [...l.candidates, ...fresh] };
}

/** The list after a "Show more" request for `requested` failed; ignored when the filter changed. */
export function failPage(l, error, requested) {
  if (l.status !== 'ok' || l.filter !== requested) return l;
  return { status: 'error', error };
}
